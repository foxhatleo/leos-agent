#!/usr/bin/env python3
"""Record child completions; prompt a leo-* worker once for its missing contract lines.

  observe_agent.py                       a lifecycle hook: event JSON on stdin
  observe_agent.py --reconcile SID PATH  the detached SessionEnd backfill
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dispatch_guard
import dispatch_log
import outcome
import session_models

# Lifecycle events spell the child's fields several ways across harness
# versions, exactly as dispatch events do. Reading one spelling left most
# SubagentStop rows with no agent and no model at once -- 70 of 84 in one
# day's log -- while the transcript-side scan attributed the same agents
# cleanly. The agent name shares the guard's tuple so the two cannot drift.
AGENT_ID_KEYS = ("agent_id", "agentId")
CALL_KEYS = ("tool_use_id", "toolUseId", "tool_call_id", "toolCallId", "call_id", "callId")
SESSION_KEYS = ("session_id", "sessionId")
TRANSCRIPT_KEYS = ("transcript_path", "transcriptPath")
# The child's final text, under the harness's own spelling where one exists:
# Claude and Codex say last_assistant_message, Hermes says child_summary,
# Cursor says summary, and the JavaScript adapters build an envelope with
# result_text. It is read for its closing tokens and dropped; only enums and
# counts reach the log.
CHILD_TEXT_KEYS = ("last_assistant_message", "lastAssistantMessage", "child_summary", "summary", "result_text")
SOURCES = ("message", "transcript", "handback", "tool-output", "status-only")

# Harnesses whose SubagentStop accepts a decision that keeps the child
# running, each with a stop_hook_active flag that marks the continuation.
CONTRACT_HARNESSES = ("claude", "codex")
CONTRACT_PROMPT = (
    "leos-agent: your final message lacks the closing contract lines. Restate your final report in full, "
    "because the parent receives only your next message, and end it with `Result: <done, partial, blocked or "
    "escalate>` and `Verified: <the command or evidence you ran, or none>`. Do not redo the work."
)

# Claude Code gives every SessionEnd hook one shared 1.5 s budget, and a
# plugin hook's own timeout cannot raise it. The hook therefore only starts a
# detached reconciliation and returns; that process has this long.
RECONCILE_SECONDS = 20
INLINE_RECONCILE_SECONDS = 0.5


def signals(event):
    """outcome/verified/usage/turns/outcome_source for one completion event."""
    text = dispatch_guard._first_str(event, CHILD_TEXT_KEYS)[-outcome.TAIL_BYTES:]
    declared = event.get("outcome_source")
    source = declared if declared in SOURCES else ("message" if text else None)
    path = session_models.child_transcript(event)
    if not text and path:
        text = session_models.transcript_tail_text(path, outcome.TAIL_BYTES)
        source = "transcript" if text else source
    parsed = outcome.parse(text)
    if parsed["outcome"] == "unknown" and path:
        # A child that reported through the hand-back tool stops with closing
        # text; its contract lines are in the report it handed back.
        handed = outcome.parse(session_models.transcript_handback_text(path, outcome.TAIL_BYTES))
        if handed["outcome"] != "unknown" or handed["verified"] is not None:
            parsed, source = handed, "handback"
    usage = outcome.usage_from(event.get("usage"))
    complete, turns = usage is not None, None
    if usage is None and path:
        stats = session_models.transcript_stats(path)
        usage, complete, turns = stats["usage"], stats["complete"], stats["turns"]
    if not text and source is None and event.get("status") is not None:
        source = "status-only"
    return {"outcome": parsed["outcome"], "verified": parsed["verified"], "outcome_source": source,
            "usage": usage, "usage_complete": bool(complete) if usage is not None else None, "turns": turns}


def contract_prompt(event, harness):
    """The one continuation that asks a leo-* worker for its contract lines, or None.

    Only where the harness's SubagentStop can keep the child running, only for
    this plugin's tier agents, and only on a first stop: stop_hook_active must
    be present and false, so a continuation can never prompt again. A child
    that handed its report back through the hand-back tool has already
    delivered it and is never prompted. Reads the event and at most bounded
    transcript tails.
    """
    if harness not in CONTRACT_HARNESSES or event.get("stop_hook_active") is not False:
        return None
    if os.environ.get("LEOS_AGENT_DISPATCH_GUARD", "on").strip().lower() != "on":
        return None
    from routing_engine import tier_for
    if tier_for(dispatch_guard._first_str(event, dispatch_guard.AGENT_KEYS)) is None:
        return None
    text = event.get("last_assistant_message")
    if not isinstance(text, str):
        return None  # absent or null: nothing to judge
    parsed = outcome.parse(text[-outcome.TAIL_BYTES:])
    if parsed["outcome"] != "unknown" and parsed["verified"] is not None:
        return None
    path = session_models.child_transcript(event)
    if session_models.transcript_handback_text(path, 1):
        return None
    if harness == "claude" and event.get("permission_mode") == "auto":
        # Claude provides the hand-back tool only in auto mode, and the child
        # transcript can lag the stop event. Prompt only once the transcript
        # holds the final message, so a missing hand-back is a real absence.
        closing = text.strip()[-200:]
        if not closing or closing not in session_models.transcript_tail_text(path, outcome.TAIL_BYTES):
            return None
    if harness == "codex":
        return {"decision": "block", "reason": CONTRACT_PROMPT}
    return {"hookSpecificOutput": {"hookEventName": "SubagentStop", "additionalContext": CONTRACT_PROMPT}}


def reconcile(session_id, transcript, harness="claude", seconds=RECONCILE_SECONDS):
    """Backfill children whose transcripts flushed after their SubagentStop.

    Claude may flush the child's response only after SubagentStop. Every
    lifecycle row of this session that still lacks a model, or still lacks an
    outcome, is read again from the child's own transcript, once, within
    `seconds`.
    """
    deadline = time.monotonic() + seconds
    session = dispatch_log.digest(session_id)
    if not session or not transcript:
        return
    rows = [row for row in dispatch_log.read() if row.get("session") == session and row.get("harness") == harness
            and row.get("decision") in dispatch_log.COMPLETION and row.get("agent_id")]
    latest = {}
    for row in rows:
        latest[row["agent_id"]] = row  # the newest report of each child
    for agent, row in latest.items():
        if time.monotonic() > deadline:
            return
        missing_model = row.get("decision") != "executed"
        if not missing_model and row.get("outcome") not in (None, "unknown"):
            continue
        # child_transcript refuses ids that are not safe path components.
        child = {"agent_id": agent, "transcript_path": transcript}
        model = session_models.transcript_model(session_models.child_transcript(child))
        if not model:
            continue
        late = signals(child)
        # Backfill only what the SubagentStop row lacked: a child that flushed
        # after its stop event had no text to classify then.
        fill = {k: v for k, v in late.items() if row.get(k) in (None, "unknown")}
        if fill.get("outcome") not in (None, "unknown"):
            # An outcome travels with the evidence state and source it came from.
            fill.update(verified=late["verified"], outcome_source=late["outcome_source"])
        if not missing_model and fill.get("outcome") in (None, "unknown"):
            continue  # nothing new to say
        # The row keeps the child's own stop time, which is what the join orders by.
        dispatch_log.append({**row, **fill, "decision": "executed", "effective_model": row.get("effective_model") or model,
                             "reason": "session-end-child-transcript-model" if missing_model else "session-end-late-outcome"})


def reconcile_later(event):
    """Start the SessionEnd backfill detached, so the hook returns within its budget."""
    session = dispatch_guard._first_str(event, SESSION_KEYS)
    transcript = dispatch_guard._first_str(event, TRANSCRIPT_KEYS)
    if not session or not transcript:
        return
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--reconcile", session, transcript],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError:
        reconcile(session, transcript, seconds=INLINE_RECONCILE_SECONDS)


def observe(event, harness):
    """Handle one lifecycle event; returns the hook's JSON reply, or None for `{}`."""
    kind = event.get("hook_event_name")
    if kind == "SessionEnd" and harness == "claude":
        reconcile_later(event)
        return None
    if kind == "PostModelSwitch":
        session_models.remember(event, harness)
        return None
    if kind != "SubagentStop":
        return None
    if harness == "claude" and event.get("agent_type") == "":
        # Claude's own internal agents (prompt suggestions, side questions)
        # stop with an empty agent type; no dispatch started them.
        return None
    try:
        prompt = contract_prompt(event, harness)
    except Exception as exc:  # noqa: BLE001 - the prompt is optional; the record is not
        dispatch_guard._breadcrumb(harness, exc)
        prompt = None
    if prompt:
        return prompt  # the child continues; its final stop writes the record
    # Parent transcript/model fields belong to the parent in lifecycle events.
    # Only the child's own transcript can establish its actual response model,
    # or an adapter that read it from the host's own record of the child run.
    # When the event does not name that transcript, derive it from agent_id
    # now rather than leaving the row unattributed until SessionEnd.
    model = session_models.transcript_model(session_models.child_transcript(event))
    declared = event.get("child_model") if isinstance(event.get("child_model"), str) and event["child_model"] else None
    reason = event.get("reason") if isinstance(event.get("reason"), str) else None
    dispatch_log.append({"v": dispatch_log.RECORD_VERSION,
                         "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "harness": harness, "decision": "executed" if model or declared else "completed",
                         "reason": reason or ("child-transcript-model" if model else "child-model-unavailable"),
                         "effective_model": model or declared,
                         "agent": dispatch_guard._first_str(event, dispatch_guard.AGENT_KEYS) or None,
                         "agent_id": dispatch_guard._first_str(event, AGENT_ID_KEYS) or None,
                         "session": dispatch_log.digest(dispatch_guard._first_str(event, SESSION_KEYS)),
                         "call_id": dispatch_guard._first_str(event, CALL_KEYS) or None,
                         "status": event.get("status") if isinstance(event.get("status"), str) else None,
                         **signals(event)})
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--reconcile"]:
        try:
            reconcile(argv[1], argv[2])
        except Exception as exc:  # noqa: BLE001 - detached; nobody reads its exit code
            dispatch_guard._breadcrumb("claude", exc)
        return 0
    event, reply = {}, None
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("hook input exceeded 2 MiB")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        reply = observe(event, dispatch_guard.harness(event))
    except Exception as exc:
        dispatch_guard._breadcrumb(dispatch_guard.harness(event), exc)
    # Codex lifecycle hooks require JSON. Nothing is injected into the parent;
    # the only non-empty reply is the one-time contract prompt to the child.
    print(json.dumps(reply or {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
