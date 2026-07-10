#!/usr/bin/env python3
"""Cursor beforeShellExecution shim around the shared bash-guard.py.

Cursor's hook contract differs from Claude/Codex (JSON permission verdict, not exit-43). This
adapter reads Cursor's hook JSON on stdin, runs the single shared guard, and emits a Cursor
`deny` verdict for the catastrophic-deletion class — otherwise it stays silent (no opinion; Cursor's
own permission system decides). Symlinked into ~/.cursor/hooks/ from the clone; self-locates the
guard via realpath so it stays single-source. Registered with failClosed:true so a crash blocks.
"""

import json
import os
import subprocess
import sys

# tools/cursor/guard-adapter.py -> clone root -> core/hooks/bash-guard.py
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
GUARD = os.path.join(_ROOT, "core", "hooks", "bash-guard.py")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # can't parse -> no opinion (Cursor decides); failClosed guards true crashes
    command = data.get("command") or (data.get("tool_input") or {}).get("command") or ""
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
    if not command:
        return 0
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})
    launcher = os.path.join(_ROOT, "bin", "leos-python")
    try:
        r = subprocess.run([launcher, GUARD], input=payload, capture_output=True,
                           text=True, timeout=10)
    except Exception:
        reason = "Leo's Agents guard is unavailable; refusing shell execution"
        print(json.dumps({"permission": "deny", "userMessage": reason, "agentMessage": reason}))
        return 0
    if r.returncode == 43:
        reason = (r.stderr or "blocked by bash-guard").strip()
        print(json.dumps({"permission": "deny", "userMessage": reason,
                          "agentMessage": reason}))
        return 0
    if r.returncode != 0:
        reason = "Leo's Agents guard failed; refusing shell execution"
        print(json.dumps({"permission": "deny", "userMessage": reason, "agentMessage": reason}))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
