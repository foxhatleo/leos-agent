#!/usr/bin/env python3
"""Record observed child models without injecting text or continuing a child."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dispatch_guard
import dispatch_log
import session_models


def observe(event, harness):
    kind = event.get("hook_event_name")
    if kind == "PostModelSwitch":
        session_models.remember(event, harness)
        return
    if kind != "SubagentStop":
        return
    # Parent transcript/model fields belong to the parent in lifecycle events.
    # Only the child's own transcript can establish its actual response model.
    model = session_models.transcript_model(event.get("agent_transcript_path"))
    dispatch_log.append({"v": dispatch_log.RECORD_VERSION,
                         "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "harness": harness, "decision": "executed" if model else "completed",
                         "reason": "child-transcript-model" if model else "child-model-unavailable",
                         "effective_model": model, "agent": event.get("agent_type"),
                         "agent_id": event.get("agent_id"),
                         "session": dispatch_log.digest(event.get("session_id")),
                         "call_id": event.get("tool_use_id") or event.get("call_id")})


def main():
    event = {}
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("hook input exceeded 2 MiB")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        observe(event, dispatch_guard.harness(event))
    except Exception as exc:
        dispatch_guard._breadcrumb(dispatch_guard.harness(event), exc)
    # Codex lifecycle hooks require JSON; no context or continuation fields.
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
