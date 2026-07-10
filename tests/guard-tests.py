#!/usr/bin/env python3
"""Tests for core/hooks/bash-guard.py — the catastrophic-deletion tripwire.

Each case runs the real guard as a subprocess with a JSON payload on stdin and asserts the exit
code (43 = block, 0 = allow). Run: bin/leos-python tests/guard-tests.py
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
GUARD = os.path.join(ROOT, "core", "hooks", "bash-guard.py")
HOME = os.path.realpath(os.path.expanduser("~"))

# (command, cwd) -> should block
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
    "git status",
    "echo rm -rf /",                   # not actually rm (echo)
    "chmod +x ./script.sh",            # non-recursive chmod
    "chmod -R 755 ./mydir",            # recursive chmod of a project dir
    "echo then rm -rf /",              # shell keywords as data are not executable positions
]


def run(command, cwd=None):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})
    r = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True, text=True)
    return r.returncode


def main():
    passed = failed = 0
    cwd = os.path.join(HOME, "project")   # a plausible working dir under HOME
    for cmd in BLOCK:
        ec = run(cmd, cwd)
        if ec == 43:
            passed += 1
        else:
            failed += 1
            print(f"FAIL [expected BLOCK] exit={ec}: {cmd!r}")
    for cmd in ALLOW:
        ec = run(cmd, cwd)
        if ec == 0:
            passed += 1
        else:
            failed += 1
            print(f"FAIL [expected ALLOW] exit={ec}: {cmd!r}")
    total = passed + failed
    print(f"guard-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
