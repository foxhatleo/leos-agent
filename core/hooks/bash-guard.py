#!/usr/bin/env python3
"""PreToolUse guard for Bash: blocks the catastrophic-deletion command class.

Narrow tripwire for irreversible, home/system-scale damage — NOT a general command
policy (the host's permission classifier handles that) — plus write primitives smuggled
through commands the policy layer pre-approves as read-only (git --output, git branch
mutations), because the hosts' allow vocabularies cannot express flag exclusions.
False positives are cheap (the agent sees the reason and rephrases or asks); false
negatives are not.

Exit 0 = allow. Exit 43 = block — the host's hook wrapper maps 43 -> 2 (deny) and any
other non-zero -> 2 (deny) too, so interpreter/script failures (python exits 1/2 on its
own errors) fail CLOSED: a broken guard denies the command rather than letting a
catastrophic deletion through. (main() itself still returns 0 on an unparseable payload
or a non-Bash tool_name, since those are not guardable commands; the wrappers' fail-closed
behavior covers genuine internal errors during a check.)

Tool-neutral: one script serves Claude Code, Codex, OpenCode, and Cursor. It self-
locates its optional machine-local config at <repo>/local/guard-config.json (the repo
root is found via realpath(__file__)), overridable with $LEOS_GUARD_CONFIG.

Accepted out-of-scope (other layers' job): obfuscation via scripts/eval/base64,
network exfiltration. `find -delete` and `find -exec rm` ARE covered (see check_find).
"""

import json
import os
import pwd
import re
import shlex
import sys

HOME = os.path.realpath(os.path.expanduser("~"))


def _repo_local(name):
    """Path to <repo>/local/<name>, located relative to this script even when it is
    invoked through a symlink from a tool home. core/hooks/bash-guard.py -> repo root."""
    here = os.path.realpath(__file__)
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    return os.path.join(root, "local", name)


WRAPPERS = {"sudo", "command", "env", "nice", "nohup", "time", "doas", "exec"}
CONTROL_PREFIXES = {"if", "then", "elif", "else", "while", "until", "for", "select", "do", "case"}
RECURSIVE_SHORT = re.compile(r"^-[a-zA-Z]*[rR]")
FORCEABLE = re.compile(r"^-[a-zA-Z]*f")

CRITICAL_DIRS = {
    "/", "/Users", "/home", "/root", "/dev", "/bin", "/boot", "/etc", "/lib",
    "/lib64", "/sbin", "/usr", "/var", "/opt", "/System", "/Library",
    "/Applications", "/private", "/private/etc", HOME,
}
def _home_toplevel():
    """OS-standard home dirs + machine extras from optional guard-config.json
    ({"homeToplevel": ["projects", ...]}). Config errors are ignored (fail-open)."""
    dirs = {"Desktop", "Documents", "Downloads", "Library", "Pictures", "Movies", "Music"}
    cfg_path = os.environ.get("LEOS_GUARD_CONFIG", _repo_local("guard-config.json"))
    try:
        with open(cfg_path) as f:
            extra = json.load(f).get("homeToplevel", [])
        dirs |= {d for d in extra if isinstance(d, str) and d and "/" not in d}
    except Exception:
        pass
    return {os.path.join(HOME, d) for d in dirs}


HOME_TOPLEVEL = _home_toplevel()
HOME_REF = re.compile(r"(~([A-Za-z_][\w-]*)?(/|[\s*]|$)|\$\{?HOME\}?)")
WATCHED = {"rm", "dd", "chmod", "xargs", "cd", "git", "find"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
UNKNOWN_DIR = "<unknown>"
UNKNOWN_PATH = "<unexpanded-shell-path>"

# Whole subtrees that are never rm -rf'd unattended. /var excepted for temp dirs;
# home containers (/Users, /home) handled separately so the caller's OWN home
# subtree stays allowed while OTHER users' home trees stay critical. /private is the
# macOS backing store for /etc, /var, /tmp (which are symlinks into it), so its
# subtree is critical except for the temp-dir exemptions above.
PREFIX_CRITICAL = ("/bin", "/boot", "/etc", "/lib", "/lib64", "/sbin", "/usr",
                   "/System", "/Library", "/Applications", "/dev", "/root", "/private")
PREFIX_EXEMPT = ("/var/folders", "/var/tmp", "/private/var/folders", "/private/tmp")


def tokenize(segment):
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.split()
    cleaned = []
    for i, token in enumerate(tokens):
        if token in ("(", ")", "{", "}"):
            continue
        if i == 0:
            token = token.lstrip("({")
        if i == len(tokens) - 1:
            token = token.rstrip(")")
            if not ("{" in token and "}" in token):
                token = token.rstrip("}")
        if token:
            cleaned.append(token)
    return cleaned


def split_statements(command):
    """Split on ; && || & newline into statements; a statement may contain a pipeline.

    Backslash-newline line continuation is joined first: `rm -rf \\\n~` is one statement
    in bash (the backslash escapes the newline), so splitting on the raw newline would
    detach the target from `rm -rf` and let a recursive delete slip past check_rm."""
    command = re.sub(r"\\\r?\n", " ", command)
    return [s for s in re.split(r"(?:\|\||&&|[;&\n])", command) if s.strip()]


def split_pipeline(statement):
    return [s for s in statement.split("|") if s.strip()]


def strip_wrappers(tokens):
    """Strip leading VAR=val assignments; if a wrapper (sudo/env/...) leads, scan
    forward to the first WATCHED command so wrapper flags AND their operands
    (e.g. `sudo -u root rm`) can't shield the real command."""
    i = 0
    while i < len(tokens) and ASSIGN_RE.match(tokens[i]):
        i += 1
    tokens = tokens[i:]
    if not tokens:
        return []
    first = os.path.basename(tokens[0])
    function_prefix = bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\(\)\{?", tokens[0]))
    if first in WRAPPERS or first in CONTROL_PREFIXES or function_prefix:
        for j in range(1, len(tokens)):
            if os.path.basename(tokens[j]) in WATCHED or tokens[j].startswith("mkfs"):
                return tokens[j:]
        return []
    return tokens


def _subst_var(text, name, value):
    """Boundary-aware $VAR/${VAR} substitution: never rewrites a longer variable that merely
    shares the prefix ($PWD_old, $HOME2, ...) — those must stay unresolved so the caller's
    conservative unknown-path block fires instead of a mis-expanded concrete path."""
    return re.sub(r"\$\{" + name + r"\}|\$" + name + r"(?![A-Za-z0-9_])", lambda _m: value, text)


def expand(target, cwd, cd_context):
    """Expand only stable shell path values and resolve relative paths against cd-context/tool cwd.

    ``$PWD`` is deliberately modelled from the statement's preceding ``cd`` rather than the hook
    process's own environment; that closes ``cd / && rm -rf $PWD``.  Other shell expansion is not
    safe to guess for a catastrophic operation, so it remains an explicit unknown for the caller
    to block conservatively.
    """
    if cd_context in (UNKNOWN_DIR, UNKNOWN_PATH):
        # `$PWD` follows the shell's last successful `cd`; falling back to the hook payload's
        # original cwd here would mis-model `cd $UNKNOWN && rm -rf $PWD` as a safe project delete.
        workdir = None
    else:
        workdir = cd_context or cwd
    t = _subst_var(target, "HOME", HOME)
    if workdir:
        t = _subst_var(t, "PWD", workdir)
    elif "$PWD" in t or "${PWD}" in t:
        return UNKNOWN_PATH
    if "$" in t or "`" in t or "$(`" in t:
        return UNKNOWN_PATH
    if t == "~" or t.startswith("~/"):
        t = HOME + t[1:]
    else:
        m = re.match(r"^~([A-Za-z_][\w-]*)(/.*)?$", t)
        if m:  # ~user expansion: macOS + Linux home containers
            try:
                t = pwd.getpwnam(m.group(1)).pw_dir + (m.group(2) or "")
            except KeyError:
                t = "/Users/" + m.group(1) + (m.group(2) or "")
    base = cd_context or cwd
    if base in (UNKNOWN_DIR, UNKNOWN_PATH):
        base = None
    if t and not t.startswith("/") and base:
        t = os.path.join(base, t)
    return os.path.realpath(t) if t.startswith("/") else t


def brace_variants(path):
    """Small shell-brace model for critical-path checks; one comma group is enough for rm args."""
    m = re.search(r"\{([^{}]+)\}", path)
    if not m:
        return [path]
    parts = [p for p in m.group(1).split(",") if p]
    if not parts:
        return [path]
    return [path[:m.start()] + p + path[m.end():] for p in parts]


def is_critical(path):
    """Is path a critical dir, inside a critical subtree, or a glob over one's contents?"""
    if not path:
        return False
    starred = path.endswith(("/*", "/.*")) or path in ("/*", "*")
    norm = os.path.normpath(re.sub(r"/\.?\*$", "", path)) if starred else os.path.normpath(path)
    if norm in CRITICAL_DIRS or norm in HOME_TOPLEVEL:
        return True
    # The caller's OWN home subtree is exempt (routine project deletes are allowed).
    if norm.startswith(HOME + "/"):
        return False
    if norm.startswith("/") and not any(
            norm == e or norm.startswith(e + "/") for e in PREFIX_EXEMPT):
        for p in PREFIX_CRITICAL:
            if norm == p or norm.startswith(p + "/"):
                return True
        # /var and its macOS alias /private/var (temp dirs exempted above)
        if norm == "/var" or norm.startswith("/var/") \
                or norm == "/private/var" or norm.startswith("/private/var/"):
            return True
        # ANY OTHER user's home tree (root or any depth) under /Users or /home.
        # The caller's own home was already exempted above, so this only fires for
        # other users' homes — critical on shared boxes, at any depth.
        if re.match(r"^/(Users|home)/[^/]+(/.*)?$", norm):
            return True
    return False


def statement_has_critical_literal(statement, cwd, cd_context):
    """Detect literal critical paths fed through a pipeline into xargs rm -r."""
    for token in tokenize(statement):
        normalized = token.replace("\\n", "\n").replace("\\0", "\0")
        for frag in re.split(r"[\s\x00]+", normalized):
            if not frag or frag.startswith("-") or "%" in frag:
                continue
            for candidate in brace_variants(expand(frag, cwd, cd_context)):
                if is_critical(candidate):
                    return True
    return False


def check_rm(tokens, cwd, cd_context):
    """tokens = wrapper-stripped command tokens with tokens[0] ~ rm."""
    recursive = False
    targets = []
    for t in tokens[1:]:
        if t == "--no-preserve-root":
            return "rm --no-preserve-root"
        if t in ("--recursive", "-R"):
            recursive = True
        elif t.startswith("--"):
            continue
        elif t.startswith("-"):
            if RECURSIVE_SHORT.match(t):
                recursive = True
        else:
            targets.append(t)
    if not recursive:
        return None
    for raw in targets:
        # Unknown working directory: relative sweeps are unverifiable — block conservatively.
        if raw in (".", "..", "./", "../", "./*", "../*", "*") and not (cwd or cd_context):
            return f"recursive rm of '{raw}' with unknown working directory"
        for candidate in brace_variants(expand(raw, cwd, cd_context)):
            if candidate == UNKNOWN_PATH:
                return f"recursive rm with unexpanded shell path '{raw}'"
            if is_critical(candidate):
                return f"recursive rm targeting '{raw}'"
    return None


def check_find(tokens, cwd, cd_context, statement):
    """find -delete and find -exec/-ok/-execdir/-okdir rm are recursive deletes wearing a find
    hat; block when any path operand (after expand) is critical or unexpanded/unknown. Bare
    `find -delete` with no path (cwd-relative) stays allowed when cwd is a known project dir,
    matching the rm branch's cwd handling. Path operands are expanded and classified via
    is_critical (the caller's own home subtree is exempt there), so ~/$HOME spellings of the
    caller's own project behave like `rm -rf ~/project/...` rather than false-blocking."""
    rest = tokens[1:]
    deleting = False
    i = 0
    while i < len(rest):
        t = rest[i]
        if t == "-delete":
            deleting = True
        elif t in ("-exec", "-ok", "-execdir", "-okdir"):
            # The token after the -exec* predicate (skipping find's own flags) is the command.
            j = i + 1
            while j < len(rest) and rest[j].startswith("-") and rest[j] not in (";", "+"):
                j += 1
            if j < len(rest) and os.path.basename(rest[j]) == "rm":
                deleting = True
        i += 1
    if not deleting:
        return None
    # Collect path operands: non-flag tokens that are not the find predicate payload. A bare
    # `find -delete` (no path) sweeps cwd; that is unverifiable without a known cwd.
    path_operands = []
    has_path_operand = False
    for t in rest:
        if t.startswith("-") or t in (";", "+") or t.startswith("{}"):
            continue
        has_path_operand = True
        path_operands.append(t)
    if not has_path_operand and not (cwd or cd_context):
        return "find -delete / find -exec rm with unknown working directory"
    for raw in path_operands:
        for candidate in brace_variants(expand(raw, cwd, cd_context)):
            if candidate == UNKNOWN_PATH:
                return f"find delete/exec with unexpanded shell path '{raw}'"
            if is_critical(candidate):
                return f"find -delete / find -exec rm targeting '{raw}'"
    return None


GIT_OUTPUT_SUBS = {"diff", "log", "show"}
GIT_GLOBAL_VALUED = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
BRANCH_MUTATING_LONG = {"--delete", "--force", "--move", "--copy", "--edit-description",
                        "--unset-upstream"}
BRANCH_MUTATING_SHORT = re.compile(r"^-[a-zA-Z]*[dDmMcCf]")


def check_git(tokens):
    """Write primitives smuggled through pre-approved read-only git commands: the host allow
    vocabularies are prefix-based and cannot exclude flags, so the guard closes --output on the
    diff family and the mutating git-branch forms. Branch CREATION is reversible and stays out
    of scope."""
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in GIT_GLOBAL_VALUED:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= len(tokens):
        return None
    sub, rest = tokens[i], tokens[i + 1:]
    if sub in GIT_OUTPUT_SUBS:
        for t in rest:
            if t == "--output" or t.startswith("--output="):
                return f"git {sub} --output writes an arbitrary file from a pre-approved read-only command"
    if sub == "branch":
        for t in rest:
            if t in BRANCH_MUTATING_LONG or t.startswith("--set-upstream-to") \
                    or (t.startswith("-") and not t.startswith("--") and BRANCH_MUTATING_SHORT.match(t)):
                return "git branch with a mutating/deleting flag"
    return None


def handle_cd(tokens, cwd, cd_context):
    """Model cd: bare cd -> HOME; `cd -` -> unknown; skip flags/--."""
    args = [t for t in tokens[1:] if not (t.startswith("-") and t != "-")]
    if not args:
        return HOME
    if args[0] == "-":
        return UNKNOWN_DIR
    result = expand(args[0], cwd, cd_context)
    return UNKNOWN_DIR if result == UNKNOWN_PATH else result


def check_statement(statement, cwd, cd_context):
    """Check one statement (possibly a pipeline). Returns (reason|None, new_cd_context)."""
    stmt_home_ref = bool(HOME_REF.search(statement))

    for stage in split_pipeline(statement):
        tokens = strip_wrappers(tokenize(stage))
        if not tokens:
            continue
        cmd = os.path.basename(tokens[0])

        if cmd == "cd":
            cd_context = handle_cd(tokens, cwd, cd_context)
            continue
        if cmd == "rm":
            reason = check_rm(tokens, cwd, cd_context)
            if reason:
                return reason, cd_context
        if cmd == "find":
            reason = check_find(tokens, cwd, cd_context, statement)
            if reason:
                return reason, cd_context
        if cmd == "xargs":
            # Scan past xargs flags/operands (-0, -n 1, -I{}, --no-run-if-empty...) to find rm.
            rest = tokens[1:]
            for j, t in enumerate(rest):
                if os.path.basename(t) == "rm":
                    # `find /etc | xargs rm` (no -r) deletes every file fed in — the recursion
                    # flag is irrelevant when the pipeline already carries a critical/home path,
                    # so the -r gate is intentionally dropped here.
                    if stmt_home_ref or statement_has_critical_literal(
                            statement, cwd, cd_context):
                        return "piped xargs rm with a home/root reference in the pipeline", cd_context
                    break
        if cmd == "mkfs" or cmd.startswith("mkfs."):
            return "mkfs (filesystem format)", cd_context
        if cmd == "dd":
            for t in tokens[1:]:
                if t.startswith("of=/dev/"):
                    return "dd writing to a raw device", cd_context
        if cmd == "chmod":
            rec = any(RECURSIVE_SHORT.match(t) or t == "--recursive" for t in tokens[1:] if t.startswith("-"))
            if rec:
                for t in tokens[1:]:
                    if t.startswith("-"):
                        continue
                    for candidate in brace_variants(expand(t, cwd, cd_context)):
                        if is_critical(candidate):
                            return "recursive chmod on /, home, or system path", cd_context
        if cmd == "git":
            reason = check_git(tokens)
            if reason:
                return reason, cd_context
    return None, cd_context


def check(command, cwd):
    cd_context = None
    for statement in split_statements(command):
        reason, cd_context = check_statement(statement, cwd, cd_context)
        if reason:
            return reason
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command", "")
        if not isinstance(command, str) or not command:
            return 0
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        try:
            reason = check(command, cwd)
        except Exception:
            # Fail CLOSED: a bug inside check()/is_critical() during a guardable Bash command must
            # not let a catastrophic deletion through. The host wrappers map any non-zero -> deny,
            # so re-raise as a block rather than swallowing it as exit 0. (Non-Bash tool_names and
            # unparseable payloads are still returned 0 above, since those are not guardable.)
            sys.stderr.write(
                "[bash-guard] BLOCKED — internal error while evaluating the command. "
                "Failing closed: this command class is irreversible, so a guard fault denies it.\n"
            )
            return 43
        if reason:
            sys.stderr.write(
                f"[bash-guard] BLOCKED — {reason}. This command class is irreversible at "
                f"home/system scale and is never run unattended. If the deletion is genuinely "
                f"intended: use a narrower explicit path (never '~', '/', '.', or a home-level "
                f"directory), prefer moving to trash, or ask the user to run it themselves.\n"
            )
            return 43
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
