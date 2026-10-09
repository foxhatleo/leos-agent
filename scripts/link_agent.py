#!/usr/bin/env python3
"""Claude PostToolUse on Agent/Task: record which child a dispatch started.

SubagentStop names the child (agent_id) but not the Agent call that started
it, and PreToolUse names the call (tool_use_id) but not the child. The Agent
tool's result carries both: `agentId` for a foreground run that completed and
for one launched in the background. This hook writes one row linking the two,
so the report joins a completion to its exact dispatch instead of guessing.

Kept to the cheapest path: no transcript read, no catalog, and nothing
imported until there is a link to write. The row holds ids and a status token.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MAX_INPUT = 8 * 1024 * 1024  # a foreground result carries the child's final text
_STATUS_RE = re.compile(r"[a-z][a-z_-]{0,31}")


def link(event):
    """The link row for one PostToolUse event, or None when it names no child."""
    if not isinstance(event, dict) or event.get("hook_event_name") != "PostToolUse":
        return None
    if event.get("tool_name") not in ("Agent", "Task"):
        return None
    response = event.get("tool_response")
    call, session = event.get("tool_use_id"), event.get("session_id")
    child = response.get("agentId") if isinstance(response, dict) else None
    if not all(isinstance(value, str) and value for value in (call, session, child)):
        return None
    import dispatch_log
    status = response.get("status")
    return {"v": dispatch_log.RECORD_VERSION, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "harness": os.environ.get("LEOS_AGENT_HARNESS") or "claude", "decision": dispatch_log.LINK,
            "session": dispatch_log.digest(session), "call_id": call, "agent_id": child,
            "status": status if isinstance(status, str) and _STATUS_RE.fullmatch(status) else None}


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) <= MAX_INPUT:
            row = link(json.loads(raw))
            if row:
                import dispatch_log
                dispatch_log.append(row)
    except Exception as exc:  # noqa: BLE001 - an observer fails open
        try:
            import dispatch_guard
            dispatch_guard._breadcrumb("claude", exc)
        except Exception:  # noqa: BLE001
            pass
    return 0  # no output: nothing is added to the parent's context


if __name__ == "__main__":
    sys.exit(main())
