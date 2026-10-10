"""Bounded parent-model observations; stores identifiers, never conversation text."""
import hashlib
import json
import os
import re
from pathlib import Path
import time

import accounting
from state import _data_root, atomic_write


def _path(harness, session):
    key = hashlib.sha256((harness + ":" + session).encode()).hexdigest()
    return Path(_data_root()) / "sessions" / (key + ".json")


# One file per session, read back for a day at most (parent_model's expiry).
# Two days of history and a few hundred files bound the directory forever.
SESSION_MAX_AGE = 2 * 86400
SESSION_MAX_FILES = 256
_SESSION_FILE_RE = re.compile(r"[0-9a-f]{64}\.json")
_STALE_TMP_RE = re.compile(r"tmp\w+\.tmp")


def prune_sessions(directory, now=None):
    """Delete observations past SESSION_MAX_AGE, then all but the newest SESSION_MAX_FILES."""
    now = time.time() if now is None else now
    observations = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if _SESSION_FILE_RE.fullmatch(entry.name):
            observations.append((mtime, entry.path))
        elif _STALE_TMP_RE.fullmatch(entry.name) and now - mtime > SESSION_MAX_AGE:
            observations.append((mtime, entry.path))  # left by an interrupted write
    observations.sort(reverse=True)
    for index, (mtime, path) in enumerate(observations):
        if index >= SESSION_MAX_FILES or now - mtime > SESSION_MAX_AGE:
            try:
                os.unlink(path)
            except OSError:
                pass


def remember(event, harness):
    session = event.get("session_id") or event.get("sessionId")
    model = event.get("to_model") or event.get("model")
    if not isinstance(session, str) or not isinstance(model, str) or not model:
        return
    target = _path(harness, session)
    atomic_write(str(target), {"model": model, "observed_at": time.time()})
    prune_sessions(str(target.parent))


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


HANDBACK_READ_LIMIT = 256 * 1024
HANDBACK_TOOL = "SubagentHandback"


def transcript_handback_text(path, limit=4096):
    """The report a Claude child delivered through its hand-back tool, clamped, or "".

    A child that reports through that tool stops with closing text, if any, as
    its last message; the report is the tool call's `message` input. A call
    whose result is an error, such as one a PreToolUse hook denied, delivered
    nothing and is skipped. Reads at most HANDBACK_READ_LIMIT from the end;
    returns in memory only.
    """
    if not isinstance(path, str) or not path:
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - HANDBACK_READ_LIMIT))
            lines = handle.read().splitlines()
    except OSError:
        return ""
    refused = set()  # results follow their calls, so a backward scan meets them first
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) and entry.get("type") in ("assistant", "user") else None
        content = message.get("content") if isinstance(message, dict) else None
        for part in reversed(content if isinstance(content, list) else []):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_result" and part.get("is_error") is True:
                refused.add(part.get("tool_use_id"))
            if entry["type"] != "assistant" or part.get("type") != "tool_use" or part.get("name") != HANDBACK_TOOL:
                continue
            if part.get("id") is not None and part.get("id") in refused:
                continue
            report = part["input"].get("message") if isinstance(part.get("input"), dict) else None
            if isinstance(report, str) and report.strip():
                return report[-limit:]
    return ""


USAGE_READ_LIMIT = 16 * 1024 * 1024


def transcript_stats(path):
    """{"usage", "complete", "turns"} for one child transcript; usage None when unreadable.

    Counted by the usage scan's own rules (accounting.py), so the dispatch
    report and the usage scan agree about the same child. A turn is one model
    request. A file over USAGE_READ_LIMIT is not read; a Codex request that
    cannot be established marks the usage incomplete.
    """
    stats = {"usage": None, "complete": False, "turns": None}
    if not isinstance(path, str) or not path:
        return stats
    try:
        if os.path.getsize(path) > USAGE_READ_LIMIT:
            return stats
        with open(path, "rb") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return stats
    claude, codex = {}, []
    deltas, complete = accounting.CodexDeltas(), True
    for index, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if entry.get("type") == "assistant" else None
        values = accounting.claude_usage(message) if isinstance(message, dict) else None
        if values is not None:
            key = accounting.claude_key(entry, message, index)
            claude[key] = accounting.keep_largest(claude.get(key), values)
            continue
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
        if not payload or payload.get("type") != "token_count":
            continue
        delta, gap = deltas.step(payload)
        if gap == accounting.MISSING_BASELINE:
            complete = False
        if delta is not None and any(delta):
            codex.append(accounting.codex_request(delta))
    requests = codex or list(claude.values())
    if not requests:
        stats["complete"] = complete
        return stats
    usage = dict(zip(accounting.TOKEN_KEYS, (sum(column) for column in zip(*requests))))
    for key in ("cache_read", "cache_write"):
        if not usage[key]:
            del usage[key]
    stats.update(usage=usage, complete=complete, turns=len(requests))
    return stats


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
