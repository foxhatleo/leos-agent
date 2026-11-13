#!/usr/bin/env python3
"""Cursor lifecycle adapter, driven by the event name in argv.

Documented Cursor hook fields only:
  sessionStart   -> {"additional_context": <rendered policy>}
  subagentStart  -> {"permission": "allow"|"deny", "user_message": ...}
  subagentStop   -> {"followup_message": ...} once per subagent, else {}
  preCompact     -> {"user_message": <handoff nudge>}

Plugin hooks run from the plugin directory, except subagentStop, which runs
from the workspace root; every path here comes from this file's location.
Everything beyond the standard library is imported inside the guarded handler,
so a broken import can never turn into a blocked subagent: Cursor treats an
invalid permission response as a denial even with failClosed off, hence the
last-resort allow.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ALLOW = {"permission": "allow"}
MAX_INPUT = 2 * 1024 * 1024
FOLLOWUP = ("leos-agent: your report lacks the closing contract lines. Reply with the same full report, "
            "ending it with `Result: <done, partial, blocked or escalate>` and "
            "`Verified: <the command or evidence you ran, or none>`. Do not redo the work.")


def _str(event, *keys):
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _policy():
    import payload
    import routing_engine
    root = payload.plugin_root()
    if os.environ.get("LEOS_AGENT_PAYLOAD", "").strip().lower() == "off":
        return None
    if not (root / "rules" / "preferences.md").is_file():
        return None
    config, _error = routing_engine.load_config()
    return payload.payload_body(root, "cursor", config)


def _first_followup(session, event):
    """True once per subagent: the first call creates the marker file.

    subagentStop documents no subagent id, so the key is subagent_id when
    Cursor sends it, else a digest of the stop's own stable fields.
    """
    import hashlib
    import state
    ident = _str(event, "subagent_id") or "|".join(str(event.get(k)) for k in ("subagent_type", "task", "description"))
    name = hashlib.sha256(("cursor:%s:%s" % (session, ident)).encode("utf-8", "replace")).hexdigest() + ".json"
    directory = os.path.join(state._data_root(), "handbacks")
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.close(os.open(os.path.join(directory, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except OSError:
        return False
    return True


def handle(event):
    kind = event.get("hook_event_name")
    session = _str(event, "parent_conversation_id", "conversation_id", "session_id")
    parent = _str(event, "model_id", "model")
    if kind == "sessionStart":
        import session_models
        session_models.remember({"session_id": session, "model": parent}, "cursor")
        if os.environ.get("LEOS_AGENT_PRICE_REFRESH") != "off":
            import pricing
            pricing.refresh_background()
        body = _policy()
        return {"additional_context": body} if body else {}
    if kind == "preCompact":
        import compact_nudge
        return {"user_message": compact_nudge.MESSAGE}
    if kind == "subagentStart":
        import dispatch_guard
        result = dispatch_guard.process({
            "tool_name": "Task", "tool_input": {"subagent_type": event.get("subagent_type"), "prompt": event.get("task")},
            "effective_model": event.get("subagent_model"), "parent_model": parent,
            "session_id": session, "call_id": _str(event, "subagent_id", "tool_call_id"),
        }, "cursor")
        if result["action"] == "block":
            return {"permission": "deny", "user_message": dispatch_guard.render_block(result)}
        return dict(ALLOW)
    if kind == "subagentStop":
        import dispatch_guard
        import observe_agent
        import outcome
        summary = event.get("summary") if isinstance(event.get("summary"), str) else ""
        status = event.get("status") if isinstance(event.get("status"), str) else "unknown"
        if status == "completed" and dispatch_guard.guard_mode()[0] == "on":
            from routing_engine import tier_for
            judged = tier_for(event.get("subagent_type") or "") is not None
            if (judged and summary.strip() and not outcome.contract_met(outcome.parse(summary[-outcome.TAIL_BYTES:]))
                    and _first_followup(session, event)):
                return {"followup_message": FOLLOWUP}  # the child continues; its next stop is recorded
        child = _str(event, "subagent_id")
        observe_agent.observe({"hook_event_name": "SubagentStop", "session_id": session,
                               "subagent_type": event.get("subagent_type"), "call_id": child,
                               "agent_id": child, "summary": summary[-4096:], "status": status,
                               "reason": "cursor-subagent-summary" if summary.strip() else "cursor-lifecycle-status"}, "cursor")
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    named = argv[0] if argv else None
    permission = named == "subagentStart"
    result = None
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("hook input exceeded 2 MiB")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        if named and not event.get("hook_event_name"):
            event["hook_event_name"] = named
        permission = permission or event.get("hook_event_name") == "subagentStart"
        result = handle(event)
    except BaseException as exc:  # noqa: BLE001 - last resort: never block, never traceback
        try:
            import dispatch_guard
            dispatch_guard._breadcrumb("cursor", exc)
        except BaseException:
            pass
    if permission and not (isinstance(result, dict) and result.get("permission") in ("allow", "deny")):
        result = dict(ALLOW)
    print(json.dumps(result if isinstance(result, dict) else {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
