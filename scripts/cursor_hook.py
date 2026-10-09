#!/usr/bin/env python3
"""Cursor lifecycle adapter. No policy injection; no invented Task model field."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dispatch_guard
import dispatch_log
import session_models

# Cursor blocks a permission hook whose output is not a valid response, even
# with failClosed off, so subagentStart always answers, failures included.
ALLOW = {"permission": "allow"}


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
            # agent_message is what the model reads, so it can retry within the ceiling.
            message = dispatch_guard.render_block(result)
            return {"permission": "deny", "user_message": message, "agent_message": message}
        return dict(ALLOW)
    elif kind == "subagentStop":
        # This event proves lifecycle completion, not which model was billed and
        # not how the work ended: Cursor exposes a status token and no child text,
        # so the row says status-only rather than inventing an outcome.
        import observe_agent
        observe_agent.observe({"hook_event_name": "SubagentStop", "session_id": session,
                               "subagent_type": event.get("subagent_type"), "tool_call_id": event.get("tool_call_id"),
                               "status": event.get("status") if isinstance(event.get("status"), str) else "unknown",
                               "reason": "cursor-lifecycle-status"}, "cursor")
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # The manifest names the permission event, so even unreadable input answers.
    permission = "subagentStart" in argv
    result = None
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("hook input exceeded 2 MiB")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        permission = permission or event.get("hook_event_name") == "subagentStart"
        result = handle(event)
    except Exception as exc:
        dispatch_guard._breadcrumb("cursor", exc)
    if permission and not (isinstance(result, dict) and result.get("permission") in ("allow", "deny")):
        result = dict(ALLOW)
    if result:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
