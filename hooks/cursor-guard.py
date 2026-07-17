#!/usr/bin/env python3
"""Cursor beforeShellExecution guard: thin adapter over bash-guard.py's pure check().

Cursor's hook contract differs from Claude Code's: stdin carries {command, cwd, ...}
JSON, and the response is {"permission": "allow"|"deny", ...} written to stdout, always
exit 0 (Cursor has no separate block-via-exit-code channel here). This adapter never
duplicates bash-guard's detection logic — it imports the pure `check(command, cwd)`
function from bash-guard.py (via importlib.util.spec_from_file_location, since the
filename has a hyphen and isn't a valid module name) and translates its verdict into
Cursor's shape.

Fail-open only for genuinely unguardable input (unparseable stdin, missing/empty
command) — same as bash-guard's own non-Bash/unparseable exit 0. Fail CLOSED (deny) if
loading or calling check() raises, mirroring bash-guard's fail-closed exit 2 on an
internal error.
"""
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASH_GUARD_PATH = os.path.join(_HERE, "bash-guard.py")


def _load_bash_guard():
    spec = importlib.util.spec_from_file_location("bash_guard", _BASH_GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _blocked_message(reason):
    # Mirrors bash-guard.py's main() blocked-message wording pattern (not imported —
    # main() writes to stderr with an exit code, which doesn't fit Cursor's JSON-on-stdout
    # contract, so the wording is reproduced here instead).
    return (
        f"[bash-guard] BLOCKED — {reason}. This command class is irreversible at "
        "home/system scale and is never run unattended. If the deletion is genuinely "
        "intended: use a narrower explicit path (never '~', '/', '.', or a home-level "
        "directory), prefer moving to trash, or ask the user to run it themselves."
    )


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({"permission": "allow"}))
        return 0

    command = payload.get("command") if isinstance(payload, dict) else None
    if not isinstance(command, str) or not command:
        print(json.dumps({"permission": "allow"}))
        return 0

    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if not isinstance(cwd, str):
        cwd = None

    try:
        bash_guard = _load_bash_guard()
        reason = bash_guard.check(command, cwd)
    except Exception:
        print(json.dumps({
            "permission": "deny",
            "agent_message": (
                "[bash-guard] BLOCKED — internal error; failing closed on this "
                "irreversible command class."
            ),
        }))
        return 0

    if reason:
        msg = _blocked_message(reason)
        print(json.dumps({"permission": "deny", "user_message": msg, "agent_message": msg}))
        return 0

    print(json.dumps({"permission": "allow"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
