#!/usr/bin/env python3
"""Tests for hooks/bash-guard.py — the catastrophic-deletion tripwire.

Each case runs the real guard as a subprocess with a JSON payload on stdin and asserts the
exit code (2 = block, 0 = allow). Run: python3 -m unittest tests.test_guard -v
"""

import json
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(REPO, "hooks", "bash-guard.py")
HOME = os.path.realpath(os.path.expanduser("~"))

# (command) -> should block
BLOCK = [
    "rm -rf ~",
    "rm -rf /",
    "rm -rf /usr",
    "rm -rf /etc",
    "rm -rf $HOME",
    "rm -rf ${HOME}",
    "rm -rf ~/",
    "rm -rf ~/Documents",
    "rm -rf ~/Library",
    "(rm -rf ~)",                      # subshell wrapping
    "{ rm -rf ~; }",                   # brace group
    "cd /usr\nrm -rf bin",             # newline-separated statements
    "cd /usr && rm -rf bin",           # cd-context then relative rm
    "cd / && rm -rf $PWD",             # $PWD must follow the preceding cd, not the hook cwd
    "rm -rf $UNRESOLVED_DEST",         # ambiguous shell target is unsafe for recursive rm
    "cd $UNRESOLVED_DEST && rm -rf $PWD",  # an unresolved cd must not make $PWD look harmless
    "chmod -R 755 /usr",               # recursive chmod of system path
    "chmod -R 777 ~",                  # recursive chmod of home
    "chmod -R 777 /{bin,usr}",         # brace-expanded chmod
    "printf '/usr\\n' | xargs rm -rf", # literal-path xargs rm
    "echo ~ | xargs rm -rf",           # home-ref xargs rm
    "rm -rf /home/otheruser/deep/project",  # OTHER user's home subtree (the /home policy fix)
    "rm -rf /Users/someoneelse/stuff", # other user's home on macOS
    "sudo rm -rf /",                   # wrapper-shielded
    "env FOO=1 rm -rf /var",           # assignment + wrapper
    "rm --no-preserve-root -rf /tmp",  # explicit no-preserve-root
    "dd if=/dev/zero of=/dev/disk0",   # raw device write
    "mkfs.ext4 /dev/sda1",             # filesystem format
    "rm -Rf /System",
    "if true; then rm -rf /; fi",
    "while true; do rm -rf /usr; done",
    "for x in one; do rm -rf ~/Documents; done",
    "if rm -rf /etc; then true; fi",
    "cleanup(){ rm -rf /var; }",
    "rm -rf $HOME_old",                # longer var sharing the $HOME prefix must stay unknown
    "cd / && rm -rf $PWD_old",         # ditto for $PWD — never mis-expand to a concrete path
    "rm -rf ~root/x",                  # ~user expansion resolves to another (root) home tree
    # --- macOS /private + /private/etc subtree (symlink-resolved critical paths) ---
    "rm -rf /private",                 # /private backs /etc /var /tmp on macOS
    "rm -rf /etc/nginx",               # /etc/* resolves to /private/etc/* (was unprotected)
    "rm -rf /private/etc/ssh",
    "chmod -R 755 /private",
    # --- exec wrapper (was not stripped, unlike sudo/env/command) ---
    "exec rm -rf /",
    # --- find -delete / find -exec rm (recursive delete wearing a find hat) ---
    "find / -exec rm -rf {} +",
    "find /etc -delete",
    r"find /etc -exec rm -rf {} \;",
    "find / -delete",
    "find / -execdir rm -rf {} +",          # -execdir variant (was a bypass)
    "find /etc -okdir rm -rf {} +",         # -okdir variant (was a bypass)
    "find /Users/$USER -delete",            # unexpanded var operand (was a bypass)
    "find $(echo /) -delete",               # command-substitution operand (was a bypass)
    "find /private -delete",
    # --- xargs rm without -r (recursion flag is irrelevant for a fed critical path) ---
    "find /etc | xargs rm",
    "echo /etc/passwd | xargs rm",
    # --- chmod --recursive long form (RECURSIVE_SHORT only matched short -r/-R) ---
    "chmod --recursive 777 /",
    # --- root-level glob: strips to empty; `/*` survives coreutils --preserve-root (unlike `/`) ---
    "rm -rf /*",
    "rm -rf /.*",
    "rm -fr /*",
    "chmod -R 777 /*",
    # --- IFS / metacharacter word-split evasion: glued to the flag, hides the target from shlex ---
    "rm -rf${IFS}/",
    "rm${IFS}-rf${IFS}/",
    "rm -rf${IFS}~",
    # --- backslash-newline line continuation (bash joins; guard must not split) ---
    "rm -rf \\\n/",                    # noqa: E501  — `rm -rf \\\n/` is one statement in bash
]

ALLOW = [
    "rm -rf ~/project/node_modules",   # own home subtree — routine
    "rm -rf ~/code/app/dist",
    "rm -rf ./build",
    "rm -rf ./node_modules",
    "rm -rf /tmp/scratch",             # tmp is exempt-ish (not a critical prefix)
    "rm -rf /var/folders/xy/tmpfile",  # macOS temp
    "rm -rf /private/tmp/x",
    "rm file.txt",
    "rm -f package-lock.json",
    "ls -la /",
    # git BLOCK cases from v1 are DROPPED (git-inspection layer out of scope for this port);
    # git ALLOW cases stay as a regression check that plain git usage is never touched.
    "git status",
    "git branch",
    "git branch --list",
    "git branch -avv",
    "git diff HEAD~1",
    "git log --oneline -5",
    "git show HEAD",
    "git diff --output=/tmp/pwn",      # dropped feature: git --output write primitive now allowed
    "git branch -D main",              # dropped feature: mutating branch form now allowed
    "echo rm -rf /",                   # not actually rm (echo)
    "chmod +x ./script.sh",            # non-recursive chmod
    "chmod -R 755 ./mydir",            # recursive chmod of a project dir
    "echo then rm -rf /",              # shell keywords as data are not executable positions
    "rm -rf $HOME/project/dist",       # boundary char '/' after $HOME still expands
    "rm -rf ${HOME}/project/build",
    # find -delete / -exec rm in a known project cwd stays allowed (cwd-relative, not home-scale)
    r'find . -name "*.tmp" -delete',
    "find . -name node_modules -exec rm -rf {} +",
    r'find src -name "*.pyc" -exec rm {} +',
    "find ~/project -name '*.tmp' -delete",  # own home subtree (exempt like rm -rf ~/project/...)
    "find $HOME/project/build -delete",
]


def run(command, cwd=None, tool_name="Bash"):
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}, "cwd": cwd})
    r = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True, text=True)
    return r.returncode


def _fs_case_insensitive(path):
    try:
        base = os.path.realpath(path)
        flipped = base.upper() if base != base.upper() else base.lower()
        return flipped != base and os.path.exists(flipped) and os.path.samefile(base, flipped)
    except OSError:
        return False


class TestBlock(unittest.TestCase):
    def test_block_cases(self):
        cwd = os.path.join(HOME, "project")
        for cmd in BLOCK:
            with self.subTest(cmd=cmd):
                self.assertEqual(run(cmd, cwd), 2, f"expected BLOCK (2) for {cmd!r}")


class TestAllow(unittest.TestCase):
    def test_allow_cases(self):
        cwd = os.path.join(HOME, "project")
        for cmd in ALLOW:
            with self.subTest(cmd=cmd):
                self.assertEqual(run(cmd, cwd), 0, f"expected ALLOW (0) for {cmd!r}")


class TestCaseInsensitiveFs(unittest.TestCase):
    def test_users_case_variant(self):
        """`/users` vs `/Users`: blocks on a case-insensitive FS (default macOS), stays a
        distinct non-critical path on a case-sensitive FS (Linux CI) — assert whichever the
        running FS dictates, using the guard's own detection technique."""
        cwd = os.path.join(HOME, "project")
        expected = 2 if _fs_case_insensitive(HOME) else 0
        for cmd in ("rm -rf /users", "rm -rf /USERS", "rm -rf /users/someoneelse/stuff"):
            with self.subTest(cmd=cmd):
                self.assertEqual(run(cmd, cwd), expected)


class TestPayloadEdges(unittest.TestCase):
    def test_non_bash_tool_allowed(self):
        self.assertEqual(run("rm -rf /", tool_name="Read"), 0)

    def test_empty_command_allowed(self):
        self.assertEqual(run(""), 0)

    def test_garbage_stdin_allowed(self):
        r = subprocess.run([sys.executable, GUARD], input="not json {{{", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
