#!/usr/bin/env python3
"""Refuse, once, a leo-* worker's hand-back report that lacks its contract lines.

Claude Code's PreToolUse hook on SubagentHandback: event JSON on stdin, a hook
reply on stdout. In auto mode a worker reports through that tool rather than
through its final message, so the SubagentStop prompt in observe_agent.py
never judges the report. This judges it as it is handed back: a report from
one of this plugin's tier agents that lacks `Result:` or `Verified:` is denied,
and the denial reason, which Claude Code returns to the worker as the tool's
error, asks for the same report again with both lines.

At most once per child: the first refusal claims a marker under the data
directory that only one call can create, and a child whose marker exists is
let through whatever its report says. When no marker can be written, nothing
is refused. Guard modes `warn` and `off` disable the check. The reply is a
denial or nothing; it never grants permission. Errors fail open.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dispatch_guard  # noqa: E402
import outcome  # noqa: E402

TOOL = "SubagentHandback"
MAX_INPUT = 2 * 1024 * 1024
MARKER_DIR = "handbacks"
REFUSAL = (
    "leos-agent: this report was not delivered because it lacks the closing contract lines. Call "
    + TOOL + " again with the same full report, ending it with `Result: <done, partial, blocked or escalate>` "
    "and `Verified: <the command or evidence you ran, or none>`. Do not redo the work."
)


def agent_type(event):
    """The child's agent type: from the hook input, else from its transcript's metadata sidecar."""
    named = dispatch_guard._first_str(event, ("agent_type", "agentType"))
    if named:
        return named
    import session_models
    path = session_models.child_transcript(event)
    if not path or not path.endswith(".jsonl"):
        return ""
    try:
        with open(path[: -len(".jsonl")] + ".meta.json", encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return ""
    value = meta.get("agentType") if isinstance(meta, dict) else None
    return value if isinstance(value, str) else ""


def first_refusal(session, child):
    """True for exactly one call per child: the one that creates the child's marker."""
    import hashlib
    import session_models
    from state import _data_root
    directory = os.path.join(_data_root(), MARKER_DIR)
    name = hashlib.sha256(("claude:%s:%s" % (session, child)).encode("utf-8", "replace")).hexdigest() + ".json"
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(directory, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False  # already refused once, or a refusal that could not be remembered might repeat
    os.close(fd)
    session_models.prune_sessions(directory)  # the same age and count bounds as session observations
    return True


def refusal(event, harness):
    """The deny reply for a tier worker's first contract-less hand-back, or None to let it through.

    Cheap tests come first, so an ordinary hand-back reads no file and loads no
    routing code: only a report that lacks its lines pays for the tier lookup.
    """
    if harness != "claude" or event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") != TOOL:
        return None
    args = event.get("tool_input")
    report = args.get("message") if isinstance(args, dict) else None
    if not isinstance(report, str) or not report.strip():
        return None  # the tool refuses an empty report itself
    session = dispatch_guard._first_str(event, ("session_id", "sessionId"))
    child = dispatch_guard._first_str(event, ("agent_id", "agentId"))
    if not session or not child or dispatch_guard.guard_mode()[0] != "on":
        return None
    if outcome.contract_met(outcome.parse(report[-outcome.TAIL_BYTES:])):
        return None
    agent = agent_type(event)
    if not agent.rsplit(":", 1)[-1].startswith("leo-"):
        return None
    from routing_engine import tier_for
    if tier_for(agent) is None or not first_refusal(session, child):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": REFUSAL}}


def main():
    event, reply = {}, None
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("hook input exceeded 2 MiB")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        reply = refusal(event, dispatch_guard.harness(event))
    except Exception as exc:  # noqa: BLE001 - a broken check must never hold a report back
        dispatch_guard._breadcrumb(dispatch_guard.harness(event), exc, "handback-check-error")
        reply = None
    print(json.dumps(reply or {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
