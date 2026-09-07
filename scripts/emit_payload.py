#!/usr/bin/env python3
"""emit_payload: print Leo's operating preferences for a session-start hook.

The payload used to be rendered into each harness's global instruction file at
install time, which meant every upgrade needed a second, manual step that people
forgot. This script replaces that: the harness runs it once per session and the
payload is read live out of the plugin directory, so upgrading the plugin IS
upgrading the policy.

Reads the hook's JSON event on stdin (optional) and writes the payload to
stdout. Claude Code and Codex both add a SessionStart hook's stdout to the
session's context, so one command string serves both -- which also matters
because Codex trusts a hook by the hash of its command, and a command that
changed with every payload edit would need re-approving each time.

FAILS OPEN, SILENTLY ON STDOUT. A hook that printed a traceback would inject the
traceback into the session as context. Any error emits nothing, exits 0, and
leaves a breadcrumb in $LEOS_AGENT_LOCAL_PATH/emit-payload.log instead.

Determinism is the contract: two runs must produce byte-identical output, or the
prompt prefix stops being cacheable and every session pays a full cold write.
Nothing here may emit a timestamp, an absolute path, or git state.

Env:
  LEOS_AGENT_HARNESS   overrides harness detection
  LEOS_AGENT_ROOT      overrides the plugin root
  LEOS_AGENT_PAYLOAD   set to "off" to emit nothing
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dispatch_guard  # noqa: E402  reuse its harness detection, not a second copy
import payload  # noqa: E402
import routing  # noqa: E402
import state  # noqa: E402

LOG_NAME = "emit-payload.log"


def _breadcrumb(reason):
    """Best-effort note that a session got no payload. Never raises."""
    try:
        root = state._data_root()
        os.makedirs(root, mode=0o700, exist_ok=True)
        with open(os.path.join(root, LOG_NAME), "a", encoding="utf-8") as fh:
            fh.write(f"{reason}\n")
    except Exception:
        pass


def _event():
    """The hook's JSON event, or {} when there is nothing to read.

    A tty means someone ran this by hand; reading stdin there would hang.
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    except Exception:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main(argv=None):
    if os.environ.get("LEOS_AGENT_PAYLOAD", "").strip().lower() == "off":
        return 0
    try:
        event = _event()
        harness = dispatch_guard.harness(event)
        if harness not in routing.HARNESSES:
            _breadcrumb(f"unknown harness {harness!r}; emitted nothing")
            return 0
        root = payload.plugin_root()
        if not (root / "rules" / "preferences.md").is_file():
            _breadcrumb(f"no payload under {root}; set LEOS_AGENT_ROOT")
            return 0
        body = payload.payload_body(root, harness, routing.load())
    except SystemExit as exc:
        # payload.py exits on a malformed payload or config. A hook must not.
        _breadcrumb(f"render refused: {exc}")
        return 0
    except Exception as exc:
        _breadcrumb(f"{type(exc).__name__}: {exc}")
        return 0

    sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
