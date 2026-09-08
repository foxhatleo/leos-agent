#!/usr/bin/env python3
"""Harness-aware offline dispatch guard.

CLI: native command-hook output by default; --json emits the adapter protocol.
LEOS_AGENT_DISPATCH_GUARD=on|warn|off. Errors fail open and are logged distinctly.
"""
import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

Dispatch = collections.namedtuple(
    "Dispatch",
    "tool agent model prompt_bytes prompt_lines path_count prompt_hash prompt_head opaque",
)

# The agent-selection field is mandatory, and that is the whole trick. A rule
# keyed on "has a prompt" would refuse unrelated tools -- spawn_task takes
# {prompt, title, tldr} and nothing else -- and "has a model" is weaker still.
# Only naming an agent means "I am choosing who runs this".
AGENT_KEYS = ("subagent_type", "subagentType", "agent_type", "agentType", "agent", "subagent", "profile")
PROMPT_KEYS = ("prompt", "brief", "instructions", "task", "message", "input")
MODEL_KEYS = ("model", "model_id", "modelId", "model_name")

TOOL_KEYS = ("tool_name", "toolName", "tool", "name")
INPUT_KEYS = ("tool_input", "toolInput", "arguments", "args", "input", "params", "parameters")

# Third-party MCP tools are not routed by this policy and never will be, and
# their argument namespaces are outside our control forever. One prefix test
# removes the entire class of false positives they would otherwise create.
SKIP_PREFIXES = ("mcp__",)

# Seeded empty on purpose: populate from field reports, not from guesses.
SKIP_TOOLS = frozenset()

# Dispatch tools that select behaviour by model rather than by naming an agent,
# so the agent-key rule alone would never see them. Exact names only, and only
# ones observed in a real rollout: Codex's spawn_agent takes
# {task_name, message, fork_turns, model, reasoning_effort} -- task_name is a
# free-text label, so `model` is the entire routing decision there.
DISPATCH_TOOLS = ("spawn_agent",)

# Tools whose brief does not arrive as readable text. Codex encrypts `message`,
# so its length is a proxy at best and its hash changes on every re-send. Both
# the size heuristic and the conversion hash are suppressed rather than reported
# as if they meant something.
OPAQUE_BRIEF_TOOLS = ("spawn_agent",)

ALLOW, BLOCK = "allow", "block"

# Path-ish tokens in a brief. Scanning is capped at the first 8 KiB -- the
# feature does not improve past that and a hot path should not read a novel.
PATH_SCAN_BYTES = 8192
PATH_RE = re.compile(r"[\w.-]+/[\w./-]+|[\w-]+\.[A-Za-z]{1,5}\b")


def _first_str(source, keys):
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _first_dict(source, keys):
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            return value
    return None


def harness(event):
    """Which harness this event came from. Best effort, and logged either way.

    The env var is the override the in-process adapters set. Otherwise the
    transcript path is the only hint a command hook gets; when it says nothing,
    assume Claude Code, the one harness whose dispatch tool is verified.
    """
    named = os.environ.get("LEOS_AGENT_HARNESS", "").strip().lower()
    if named:
        return named
    hint = ""
    if isinstance(event, dict):
        hint = _first_str(event, ("transcript_path", "transcriptPath", "cwd"))
    for name in ("codex", "cursor", "hermes", "opencode"):
        if "/." + name in hint or "/" + name + "/" in hint:
            return name
    return "claude"


def normalize(event, _harness=None):
    """event -> Dispatch, or None when this is not a subagent dispatch.

    Never raises. Five harnesses' envelopes are unverified, and a KeyError here
    would turn a routing guard into an outage.
    """
    if not isinstance(event, dict):
        return None
    tool = _first_str(event, TOOL_KEYS)
    if tool in SKIP_TOOLS or any(tool.startswith(p) for p in SKIP_PREFIXES):
        return None
    args = _first_dict(event, INPUT_KEYS)
    if args is None:
        return None

    agent = _first_str(args, AGENT_KEYS)
    prompt = _first_str(args, PROMPT_KEYS)
    if tool == "delegate_task" and args.get("action", "spawn") == "spawn":
        tasks = args.get("tasks") or ([args] if args.get("goal") else [])
        prompt = "\n".join(str(task.get("goal", "")) for task in tasks if isinstance(task, dict))
        agent = "delegate_task"
    known = tool in DISPATCH_TOOLS
    if not prompt or not (agent or known):
        return None

    from dispatch_log import digest  # local: a no-op call must not pay for this

    opaque = tool in OPAQUE_BRIEF_TOOLS
    head = prompt[:PATH_SCAN_BYTES]
    return Dispatch(
        tool=tool or "-",
        # A model-routed dispatch names no agent; label it by its tool so the log
        # reads sensibly without inventing a tier that was never selected.
        agent=agent or tool,
        model=_first_str(args, MODEL_KEYS) or None,
        prompt_bytes=0 if opaque else len(prompt.encode("utf-8", "replace")),
        prompt_lines=0 if opaque else prompt.count("\n") + 1,
        path_count=0 if opaque else len(set(PATH_RE.findall(head))),
        prompt_hash=None if opaque else digest(prompt),
        prompt_head="" if opaque else prompt[:200],
        opaque=opaque,
    )


def triviality(dispatch):
    """0..3. A features-only score; it never changes the exit code.

    Say plainly what this can and cannot see: a trivial spawn has small *work*,
    and tool input only shows the *brief*. The two failure modes happen to
    collapse -- a short brief is either work too small to deserve a cold context
    or work that is under-briefed, and the policy forbids both -- but roughly one
    in six legitimate runner dispatches will still score here. That rate is fine
    for a log line and disqualifying for a block, which is why this is only ever
    a log line.
    """
    if dispatch is None or dispatch.opaque:
        return 0
    score = 0
    if dispatch.prompt_bytes < 220:
        score += 2
    elif dispatch.prompt_bytes < 450:
        score += 1
    if dispatch.path_count == 1:
        score += 1
    if dispatch.prompt_lines == 1:
        score += 1
    return min(score, 3)


def routable(name):
    from routing_engine import CAPABILITIES
    return bool(CAPABILITIES.get(name, {}).get("model_field"))


def _is_tier(agent):
    from routing_engine import tier_for
    return tier_for(agent) is not None


def render_block(dispatch, name=None, result=None):
    retry = (result or {}).get("retry", "Retry with an explicit model within the parent price ceiling.")
    return "[leo routing] BLOCKED: " + (result or {}).get("reason", "model choice required") + ". " + retry


def render_notice(dispatch):
    return (
        "[leo routing] %d-byte brief to %s: delegation has setup overhead and "
        "costs more than an inline read. Inline it when one file answers it."
    ) % (dispatch.prompt_bytes, dispatch.agent)


def _log(entry):
    """Best effort, always. A breadcrumb that cannot be written must not break a
    dispatch, so every failure here is swallowed -- including a missing module."""
    try:
        import dispatch_log
        dispatch_log.append(entry)
    except Exception:
        pass


def process(event, name=None):
    """Shared mode handling, decision and logging for command/in-process adapters."""
    from routing_engine import route
    from session_models import parent_model
    name = name or harness(event)
    mode = os.environ.get("LEOS_AGENT_DISPATCH_GUARD", "on").strip().lower()
    result = {"action": "allow", "reason": "disabled" if mode == "off" else "not-a-dispatch", "updated_input": None}
    if mode == "off" or not isinstance(event, dict):
        return result
    tool = _first_str(event, TOOL_KEYS)
    args = _first_dict(event, INPUT_KEYS)
    try:
        parent = parent_model(event, name)
        effective = event.get("effective_model")
        if name == "claude" and os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE") == "1":
            forced = os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL") or parent
            result = route(name, tool, args, parent, effective_model=forced)
            # Forced settings cannot be overridden by updatedInput.
            if (result.get("price") or {}).get("status") == "over-ceiling":
                result.update(action="block", reason="forced-model-over-ceiling", updated_input=None,
                              retry="The forced subagent model exceeds the parent. Change the force setting or do this work locally.")
            else:
                result.update(action="allow", reason="forced-model-setting", updated_input=None)
        else:
            result = route(name, tool, args, parent, effective_model=effective, native_profiles=event.get("native_profiles"))
        if result["reason"] == "not-a-dispatch":
            return result
        dispatch = normalize(event, name)
        import dispatch_log
        action = result["action"]
        if mode == "warn" and action in ("block", "correct"):
            result.update(action="warn", proposed_action=action, updated_input=None)
        entry = dispatch_log.record(dispatch, result["action"], result["reason"], name,
                                    _first_str(event, ("session_id", "sessionId")),
                                    _first_str(event, ("cwd", "workspace", "directory")), triviality(dispatch))
        entry.update({k: result.get(k) for k in ("requested_model", "effective_model", "price", "proposed_action")})
        entry["call_id"] = event.get("tool_use_id") or event.get("call_id") or event.get("toolCallId")
        _log(entry)
        return result
    except Exception as exc:
        _breadcrumb(name, exc)
        return {"action": "allow", "reason": "guard-error", "updated_input": None}


def evaluate(event, name=None):
    result = process(event, name)
    dispatch = normalize(event, name)
    return result["action"], result["reason"], dispatch, triviality(dispatch)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("hook input exceeded 2 MiB")
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if not isinstance(event, dict):
        return 0
    name = harness(event)
    result = process(event, name)
    if "--json" in argv:
        print(json.dumps(result))
        return 0
    if result["action"] == BLOCK:
        sys.stderr.write(render_block(None, name, result) + "\n")
        return 2
    if result["action"] == "correct" and name == "claude":
        # Do not grant tool permission: update only arguments and leave the
        # harness's normal permission checks intact.
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                "updatedInput": result["updated_input"]}}))
    return 0


def _breadcrumb(name, exc):
    try:
        import dispatch_log
        dispatch_log.append({
            "v": dispatch_log.RECORD_VERSION,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "harness": name,
            "decision": "error",
            "reason": "%s: %s" % (type(exc).__name__, exc),
        })
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
