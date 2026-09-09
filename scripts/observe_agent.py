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

# Lifecycle events spell the child's fields several ways across harness
# versions, exactly as dispatch events do. Reading one spelling left most
# SubagentStop rows with no agent and no model at once -- 70 of 84 in one
# day's log -- while the transcript-side scan attributed the same agents
# cleanly. The agent name shares the guard's tuple so the two cannot drift.
AGENT_ID_KEYS = ("agent_id", "agentId")
CALL_KEYS = ("tool_use_id", "toolUseId", "call_id", "callId", "toolCallId")
SESSION_KEYS = ("session_id", "sessionId")
TRANSCRIPT_KEYS = ("transcript_path", "transcriptPath")


def observe(event, harness):
    kind = event.get("hook_event_name")
    if kind == "SessionEnd" and harness == "claude":
        # Claude may flush the child's response only after SubagentStop.
        # Reconcile bounded lifecycle records once, after the session finishes.
        session = dispatch_log.digest(dispatch_guard._first_str(event, SESSION_KEYS))
        transcript = dispatch_guard._first_str(event, TRANSCRIPT_KEYS)
        if not session or not transcript:
            return
        rows = [row for row in dispatch_log.read() if row.get("session") == session and row.get("harness") == harness]
        observed = {row.get("agent_id") for row in rows if row.get("decision") == "executed"}
        for row in rows:
            agent = row.get("agent_id")
            if row.get("decision") != "completed" or agent in observed:
                continue
            # child_transcript refuses ids that are not safe path components.
            path = session_models.child_transcript({"agent_id": agent, "transcript_path": transcript})
            if transcript_model := session_models.transcript_model(path):
                dispatch_log.append({**row, "decision": "executed", "effective_model": transcript_model,
                                     "reason": "session-end-child-transcript-model"})
                observed.add(agent)
        return
    if kind == "PostModelSwitch":
        session_models.remember(event, harness)
        return
    if kind != "SubagentStop":
        return
    # Parent transcript/model fields belong to the parent in lifecycle events.
    # Only the child's own transcript can establish its actual response model.
    # When the event does not name that transcript, derive it from agent_id
    # now rather than leaving the row unattributed until SessionEnd.
    model = session_models.transcript_model(session_models.child_transcript(event))
    dispatch_log.append({"v": dispatch_log.RECORD_VERSION,
                         "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "harness": harness, "decision": "executed" if model else "completed",
                         "reason": "child-transcript-model" if model else "child-model-unavailable",
                         "effective_model": model,
                         "agent": dispatch_guard._first_str(event, dispatch_guard.AGENT_KEYS) or None,
                         "agent_id": dispatch_guard._first_str(event, AGENT_ID_KEYS) or None,
                         "session": dispatch_log.digest(dispatch_guard._first_str(event, SESSION_KEYS)),
                         "call_id": dispatch_guard._first_str(event, CALL_KEYS) or None})


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
