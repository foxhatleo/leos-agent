"""Bounded parent-model observations; stores identifiers, never conversation text."""
import hashlib
import json
import os
import re
from pathlib import Path
import time

from state import _data_root, atomic_write


def _path(harness, session):
    key = hashlib.sha256((harness + ":" + session).encode()).hexdigest()
    return Path(_data_root()) / "sessions" / (key + ".json")


def remember(event, harness):
    session = event.get("session_id") or event.get("sessionId")
    model = event.get("to_model") or event.get("model")
    if not isinstance(session, str) or not isinstance(model, str) or not model:
        return
    atomic_write(str(_path(harness, session)), {"model": model, "observed_at": time.time()})


def transcript_model(path):
    """Read at most 1 MiB of the latest records, ignoring malformed fragments."""
    if not isinstance(path, str) or not path:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 1024 * 1024))
            lines = handle.read().splitlines()
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                message = entry.get("message") or {}
                if entry.get("type") == "assistant" and isinstance(message, dict):
                    model = message.get("model")
                    if isinstance(model, str) and model and model != "<synthetic>":
                        return model
                if entry.get("type") == "turn_context":
                    model = (entry.get("payload") or {}).get("model")
                    if isinstance(model, str) and model:
                        return model
            except (ValueError, AttributeError, TypeError):
                continue
    except OSError:
        pass
    return None


def _text_parts(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def transcript_tail_text(path, limit=4096):
    """The last assistant message's text, clamped to `limit`, or "".

    Claude records `{"type": "assistant", "message": {"content": [...]}}` and
    Codex rollouts `{"type": "response_item", "payload": {"role": "assistant",
    "content": [...]}}`. Reads at most 1 MiB from the end; returns in memory only.
    """
    if not isinstance(path, str) or not path:
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 1024 * 1024))
            lines = handle.read().splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if entry.get("type") == "assistant" else None
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
        if isinstance(message, dict):
            text = _text_parts(message.get("content"))
        elif payload and payload.get("role") == "assistant":
            text = _text_parts(payload.get("content"))
        else:
            continue
        if text.strip():
            return text[-limit:]
    return ""


USAGE_READ_LIMIT = 16 * 1024 * 1024


def transcript_usage(path):
    """(usage, complete) summed over a child's transcript; (None, False) when unreadable.

    Claude repeats one API call's usage across the records of a split turn, so
    records are deduplicated by message id. Codex reports cumulative totals in
    token_count events, so the last total wins there rather than a sum. A file
    over USAGE_READ_LIMIT is not read: (None, False) says so.
    """
    import outcome
    if not isinstance(path, str) or not path:
        return None, False
    try:
        if os.path.getsize(path) > USAGE_READ_LIMIT:
            return None, False
        with open(path, "rb") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None, False
    total, seen, codex_total = None, set(), None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if entry.get("type") == "assistant" else None
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            key = message.get("id") or id(message)
            if key in seen:
                continue
            seen.add(key)
            total = outcome.add_usage(total, outcome.usage_from(message["usage"]))
            continue
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
        if payload and payload.get("type") == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            usage = outcome.usage_from(info.get("total_token_usage") or payload.get("total_token_usage"))
            if usage:
                codex_total = usage
    return (codex_total or total), True


_AGENT_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def child_transcript(event):
    """The child's own transcript path: named by the event, or derived from agent_id.

    Claude names it as agent_transcript_path on most lifecycle events, but not
    all of them; when it is absent, the parent transcript's sibling directory
    holds subagents/agent-<id>.jsonl. None when neither is available or the id
    is not a safe path component.
    """
    path = event.get("agent_transcript_path") or event.get("agentTranscriptPath")
    if isinstance(path, str) and path:
        return path
    agent = event.get("agent_id") or event.get("agentId")
    transcript = event.get("transcript_path") or event.get("transcriptPath")
    if not (isinstance(agent, str) and _AGENT_ID_RE.fullmatch(agent) and isinstance(transcript, str) and transcript):
        return None
    filename = agent if agent.startswith("agent-") else "agent-" + agent
    return str(Path(transcript).with_suffix("") / "subagents" / (filename + ".jsonl"))


def parent_model(event, harness):
    explicit = event.get("parent_model")
    if isinstance(explicit, str) and explicit:
        return explicit
    if harness == "codex" and isinstance(event.get("model"), str) and event["model"]:
        return event["model"]  # documented active-model field, newer than transcript
    if harness == "claude" and (event.get("agent_id") or event.get("agentId")):
        # Nested review dispatches must be capped to their immediate caller,
        # never the more expensive root conversation. Claude exposes agent_id
        # in subagent hooks, while transcript_path can still name the root.
        return transcript_model(child_transcript(event))  # unknown is safer than using the root price
    # PreToolUse follows an assistant response: its transcript model is newer
    # than SessionStart and reflects per-turn provider fallback as well.
    model = transcript_model(event.get("agent_transcript_path") or event.get("transcript_path"))
    if model:
        return model
    session = event.get("session_id") or event.get("sessionId")
    if isinstance(session, str):
        try:
            data = json.loads(_path(harness, session).read_text())
            if time.time() - data["observed_at"] < 86400:
                return data["model"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return None
