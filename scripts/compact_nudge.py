#!/usr/bin/env python3
"""Codex PreCompact hook: remind the user, once per compaction, that handoff exists.

Codex surfaces a PreCompact hook's `systemMessage` as a warning in its UI; the
model does not receive it, and plain stdout is ignored. The reply is that one
field, so compaction always proceeds, and nothing is written anywhere. A
subagent's own compaction (the input carries `agent_id`) gets no reminder:
workers do not hand off. Any unreadable input means silence, never an error.

Claude Code discards a PreCompact `systemMessage`, so this is Codex only.
"""
import json
import sys

MESSAGE = ("Compacting context. To continue this work in a fresh session instead, "
           "run $leos-agent:handoff.")


def reply(event):
    if not isinstance(event, dict) or event.get("hook_event_name") != "PreCompact" or event.get("agent_id"):
        return None
    return {"systemMessage": MESSAGE}


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    answer = reply(event)
    if answer:
        sys.stdout.write(json.dumps(answer) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
