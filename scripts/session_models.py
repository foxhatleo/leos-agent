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


def parent_model(event, harness):
    explicit = event.get("parent_model")
    if isinstance(explicit, str) and explicit:
        return explicit
    if harness == "codex" and isinstance(event.get("model"), str) and event["model"]:
        return event["model"]  # documented active-model field, newer than transcript
    if harness == "claude" and event.get("agent_id"):
        # Nested review dispatches must be capped to their immediate caller,
        # never the more expensive root conversation. Claude exposes agent_id
        # in subagent hooks, while transcript_path can still name the root.
        path = event.get("agent_transcript_path")
        agent = event["agent_id"]
        transcript = event.get("transcript_path")
        if not path and isinstance(agent, str) and re.fullmatch(r"[A-Za-z0-9_-]+", agent) and isinstance(transcript, str):
            filename = agent if agent.startswith("agent-") else "agent-" + agent
            path = str(Path(transcript).with_suffix("") / "subagents" / (filename + ".jsonl"))
        return transcript_model(path)  # unknown is safer than using the root price
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
