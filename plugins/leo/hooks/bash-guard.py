#!/usr/bin/env python3
"""PreToolUse guard for Bash: blocks the catastrophic-deletion command class.

Narrow tripwire for irreversible, home/system-scale damage — NOT a general command
policy (the host's permission classifier handles that). False positives are cheap
(the agent sees the reason and rephrases or asks); false negatives are not.

Exit 0 = allow (also for a non-Bash tool_name or an unparseable/empty payload — those
are not guardable commands). Exit 2 = block; the reason is written to stderr. An
internal error while checking a Bash command also exits 2: fail CLOSED, since a
broken guard must not let a catastrophic deletion through.

Accepted out-of-scope (other layers' job): obfuscation via scripts/eval/base64,
network exfiltration, `.env`/secret-read denies. `find -delete` and `find -exec rm`
ARE covered (see check_find).
"""

import json
import os
import pwd
import re
import shlex
import sys

HOME = os.path.realpath(os.path.expanduser("~"))


def _fs_case_insensitive(path):
    """True if `path` lives on a case-insensitive filesystem (default macOS APFS/HFS+).

    os.path.realpath does NOT canonicalize case, so is_critical would compare `/users` against
    `/Users` and miss it — a one-character case change bypassing the guard on the platform it
    primarily runs on. When True, is_critical casefolds its comparisons. Fails safe to False
    (exact matching) if the probe can't run."""
    try:
        base = os.path.realpath(path)
        flipped = base.upper() if base != base.upper() else base.lower()
        return flipped != base and os.path.exists(flipped) and os.path.samefile(base, flipped)
    except OSError:
        return False


CASE_INSENSITIVE = _fs_case_insensitive(HOME)
_RE_CASE_FLAG = re.IGNORECASE if CASE_INSENSITIVE else 0


def _norm_case(text):
    """Casefold for comparison only on a case-insensitive FS (identity on POSIX/case-sensitive)."""
    return text.casefold() if CASE_INSENSITIVE else text


WRAPPERS = {"sudo", "command", "env", "nice", "nohup", "time", "doas", "exec"}
CONTROL_PREFIXES = {"if", "then", "elif", "else", "while", "until", "for", "select", "do", "case"}
RECURSIVE_SHORT = re.compile(r"^-[a-zA-Z]*[rR]")
FORCEABLE = re.compile(r"^-[a-zA-Z]*f")

CRITICAL_DIRS = {
    "/", "/Users", "/home", "/root", "/dev", "/bin", "/boot", "/etc", "/lib",
    "/lib64", "/sbin", "/usr", "/var", "/opt", "/System", "/Library",
    "/Applications", "/private", "/private/etc", HOME,
}
# OS-standard home dirs that are never rm -rf'd unattended, joined to HOME.
HOME_TOPLEVEL = {os.path.join(HOME, d) for d in
                 ("Desktop", "Documents", "Downloads", "Library", "Pictures", "Movies", "Music")}
HOME_REF = re.compile(r"(~([A-Za-z_][\w-]*)?(/|[\s*]|$)|\$\{?HOME\}?)")
WATCHED = {"rm", "dd", "chmod", "xargs", "cd", "find"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
UNKNOWN_DIR = "<unknown>"
UNKNOWN_PATH = "<unexpanded-shell-path>"

# Whole subtrees that are never rm -rf'd unattended. /var excepted for temp dirs;
# home containers (/Users, /home) handled separately so the caller's OWN home
# subtree stays allowed while OTHER users' home trees stay critical. /private is the
# macOS backing store for /etc, /var, /tmp (which are symlinks into it), so its
# subtree is critical except for the temp-dir exemptions below.
PREFIX_CRITICAL = ("/bin", "/boot", "/etc", "/lib", "/lib64", "/sbin", "/usr",
                   "/System", "/Library", "/Applications", "/dev", "/root", "/private")
PREFIX_EXEMPT = ("/var/folders", "/var/tmp", "/private/var/folders", "/private/tmp")

# Case-normalized once (identity on a case-sensitive FS, casefolded on macOS) so is_critical never
# re-casefolds the constant sets per call and Linux keeps exact matching.
_CRITICAL_DIRS_CI = {_norm_case(d) for d in CRITICAL_DIRS}
_HOME_TOPLEVEL_CI = {_norm_case(d) for d in HOME_TOPLEVEL}
_HOME_CI = _norm_case(HOME)
_PREFIX_CRITICAL_CI = tuple(_norm_case(p) for p in PREFIX_CRITICAL)
_PREFIX_EXEMPT_CI = tuple(_norm_case(p) for p in PREFIX_EXEMPT)


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
    if starred:
        # A rooted content-glob strips to empty: `/*` and `/.*` mean "everything under root", so
        # they ARE root-scale. normpath("") is "." (harmless-looking cwd) — map the emptied rooted
        # glob back to "/" instead, or `rm -rf /*` (which --preserve-root does NOT stop) slips past.
        stripped = re.sub(r"/\.?\*$", "", path)
        norm = os.path.normpath(stripped) if stripped else "/"
        if norm == "." and path.startswith("/"):
            norm = "/"
    else:
        norm = os.path.normpath(path)
    cnorm = _norm_case(norm)
    if cnorm in _CRITICAL_DIRS_CI or cnorm in _HOME_TOPLEVEL_CI:
        return True
    # The caller's OWN home subtree is exempt (routine project deletes are allowed).
    if cnorm.startswith(_HOME_CI + "/"):
        return False
    if norm.startswith("/") and not any(
            cnorm == e or cnorm.startswith(e + "/") for e in _PREFIX_EXEMPT_CI):
        for p in _PREFIX_CRITICAL_CI:
            if cnorm == p or cnorm.startswith(p + "/"):
                return True
        # /var and its macOS alias /private/var (temp dirs exempted above)
        if cnorm == "/var" or cnorm.startswith("/var/") \
                or cnorm == "/private/var" or cnorm.startswith("/private/var/"):
            return True
        # ANY OTHER user's home tree (root or any depth) under /Users or /home.
        # The caller's own home was already exempted above, so this only fires for
        # other users' homes — critical on shared boxes, at any depth. IGNORECASE only on a
        # case-insensitive FS, so `/users/other` is caught on macOS without over-blocking Linux.
        if re.match(r"^/(Users|home)/[^/]+(/.*)?$", norm, _RE_CASE_FLAG):
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
    return None, cd_context


_IFS_RE = re.compile(r"\$\{IFS(?:[:#%/^,][^}]*)?\}|\$IFS(?![A-Za-z0-9_])")


def _normalize_ifs(command):
    """Model shell IFS word-splitting so a flag-glued target can't hide from the tokenizer.

    `$IFS` / `${IFS}` / `${IFS:-...}` expand to whitespace at runtime, so `rm -rf${IFS}/` actually
    executes as `rm -rf /`. shlex.split does not expand them, leaving `-rf${IFS}/` a single token
    with no target operand — a bypass. Substitute those forms with a space BEFORE tokenizing.
    (Other obfuscation — eval/base64/hex — stays out of scope per the module docstring.)"""
    return _IFS_RE.sub(" ", command)


def check(command, cwd):
    command = _normalize_ifs(command)
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
            # not let a catastrophic deletion through. (Non-Bash tool_names and unparseable
            # payloads are still returned 0 above, since those are not guardable.)
            sys.stderr.write(
                "[bash-guard] BLOCKED — internal error while evaluating the command. "
                "Failing closed: this command class is irreversible, so a guard fault denies it.\n"
            )
            return 2
        if reason:
            sys.stderr.write(
                f"[bash-guard] BLOCKED — {reason}. This command class is irreversible at "
                f"home/system scale and is never run unattended. If the deletion is genuinely "
                f"intended: use a narrower explicit path (never '~', '/', '.', or a home-level "
                f"directory), prefer moving to trash, or ask the user to run it themselves.\n"
            )
            return 2
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
