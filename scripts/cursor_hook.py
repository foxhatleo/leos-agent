#!/usr/bin/env python3
"""Cursor lifecycle adapter. No policy injection; no invented Task model field."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dispatch_guard
import dispatch_log
import session_models


def handle(event):
    kind = event.get("hook_event_name")
    session = event.get("parent_conversation_id") or event.get("conversation_id") or event.get("session_id")
    parent = event.get("model_id") or event.get("model")
    if kind == "sessionStart":
        session_models.remember({"session_id": session, "model": parent}, "cursor")
        if os.environ.get("LEOS_AGENT_PRICE_REFRESH") != "off":
            import pricing
            pricing.refresh_background()
    elif kind == "subagentStart":
        result = dispatch_guard.process({
            "tool_name": "Task", "tool_input": {"subagent_type": event.get("subagent_type"), "prompt": event.get("task")},
            "effective_model": event.get("subagent_model"), "parent_model": parent,
            "session_id": session, "call_id": event.get("tool_call_id"),
        }, "cursor")
        if result["action"] == "block":
            return {"permission": "deny", "user_message": dispatch_guard.render_block(None, "cursor", result)}
    elif kind == "subagentStop":
        # This event proves lifecycle completion, not which model was billed.
        dispatch_log.append({"v": dispatch_log.RECORD_VERSION,
                             "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "harness": "cursor", "decision": "completed", "status": event.get("status"),
                             "session": dispatch_log.digest(session), "agent": event.get("subagent_type"),
                             "call_id": event.get("tool_call_id"), "effective_model": None})
    return None


def main():
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("hook input exceeded 2 MiB")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        result = handle(event)
        if result:
            print(json.dumps(result))
    except Exception as exc:
        dispatch_guard._breadcrumb("cursor", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
