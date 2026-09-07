#!/usr/bin/env python3
"""dispatch_guard: refuse a subagent dispatch that names no model.

WHY THIS EXISTS IN CODE RATHER THAN PROSE. The policy already said every dispatch
must name a model, and the policy was forgotten. Prose enforcement also costs
always-loaded bytes on every turn of every session, and the budget in
measure_context.py is nearly spent -- so the rule that a machine can check moved
into a hook, which costs nothing, and the payload kept only the judgment a hook
cannot make. Moving it out of the payload was what paid for the move.

WHAT IT REFUSES, AND WHAT IT DELIBERATELY DOES NOT. A dispatch that selects an
agent, carries a brief, names no model, and runs on a harness that can express
one per spawn is refused -- because on that path the harness silently inherits
the parent's expensive model, which is the failure this file exists to prevent.
Everything else is allowed. The guard NEVER picks a model: it cannot force a
cheap tier onto work that needed an expensive one, so it cannot cause a quality
regression, only an explicit choice. That is also why the block stays narrow. A
false block costs one re-dispatch turn; a caught inherited fan-out saves the cold
prefix of every child it would have spawned. The margin is wide precisely because
the rule refuses to make judgment calls, and every widening spends it.

SHAPE-BASED, NOT NAME-BASED. Only Claude Code's dispatch tool is verified; the
argument shapes on Codex, Cursor, Hermes and OpenCode are not. So the decision
turns on the arguments -- an agent-selection field plus a brief -- and an
unanticipated tool degrades to a no-op rather than a broken harness. The hook
manifests still carry a name matcher, but only as a cheap prefilter: without one,
every Read and Grep would pay a python3 spawn.

FAILS OPEN, LOUDLY. The harm here is money, not data loss. A guard that fails
closed on a broken interpreter wedges every dispatch on every harness, which is
far worse than the miss it prevents -- and on Claude Code a timed-out hook is
non-blocking anyway, so fail-closed is not even expressible there. Every internal
error allows the call and leaves a breadcrumb with decision "error", kept
rigorously distinct from a decision to allow.

  LEOS_AGENT_DISPATCH_GUARD  on (default) | warn (log, never block) | off | verbose
  LEOS_AGENT_HARNESS         overrides harness detection

Exit codes: 0 allow, 2 block (reason on stderr).
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
    """Can this harness name a model per spawn?

    Claude Code's dispatch tool takes one, and any harness Leo has configured in
    routing.json has a model to name. Everywhere else the payload itself says to
    inherit and say so -- blocking there would demand something the harness
    cannot do, a false positive by construction.
    """
    # Both take a model per spawn: Claude Code on its dispatch tool, Codex as
    # spawn_agent's `model` argument.
    if name in ("claude", "codex"):
        return True
    try:
        import routing
        return name in routing.load()
    except BaseException:
        # routing.py exits on a malformed config. A hook must never inherit that.
        return False


def _is_tier(agent):
    try:
        from dispatch_log import is_tier
        return is_tier(agent)
    except Exception:
        # Never let a missing sibling module turn a compliant dispatch into a
        # refusal: fall back to allowing anything that looks like a tier.
        return bool(agent) and agent.rsplit(":", 1)[-1].startswith("leo-")


def decide(dispatch, name, is_routable):
    """(action, reason). The entire policy, and deliberately four lines of it."""
    if dispatch is None:
        return ALLOW, "not-a-dispatch"
    if _is_tier(dispatch.agent):
        return ALLOW, "leo-tier"          # the agent definition carries the model
    if dispatch.model:
        return ALLOW, "explicit-model"    # a choice was typed; cheap or not
    if not is_routable:
        return ALLOW, "harness-cannot-route"
    return BLOCK, "no-model"


def render_block(dispatch, name=None):
    """The refusal. It must name the remedies, or it costs a turn to discover them.

    The remedies differ by harness: Codex routes by `model` on spawn_agent and
    has no agent to name, so offering it a subagent_type would be advice it
    cannot take.
    """
    if dispatch.tool in DISPATCH_TOOLS or name == "codex":
        return (
            "[leo routing] BLOCKED - %s names no model, so it would silently inherit\n"
            "the parent's. Re-dispatch with `model` set (and `reasoning_effort` with it),\n"
            "naming the economical tier's model for narrow work.\n"
            "Set LEOS_AGENT_DISPATCH_GUARD=off to disable, =warn to log only."
        ) % dispatch.tool
    return (
        '[leo routing] BLOCKED - dispatch to agent "%s" names no model, so it would\n'
        "silently inherit the parent's. Re-dispatch with one of:\n"
        '  subagent_type "leo-runner"    reading, search, tests, logs, codemods, fan-out\n'
        '  subagent_type "leo-executor"  an approved plan or a specified change\n'
        '    (a plugin install namespaces these: "leos-agent:leo-runner")\n'
        '  model: "<name>"               investigation/debugging - naming it IS the reason\n'
        "Set LEOS_AGENT_DISPATCH_GUARD=off to disable, =warn to log only."
    ) % dispatch.agent


def render_notice(dispatch):
    return (
        "[leo routing] %d-byte brief to %s: a fresh agent context is uncached and "
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


def evaluate(event, name=None):
    """(action, reason, dispatch, trivial) for one event. Shared by every adapter.

    An in-process adapter knows which harness it is and passes `name`; detection
    exists only for the command hooks, which get no say.
    """
    name = name or harness(event)
    dispatch = normalize(event, name)
    action, reason = decide(dispatch, name, routable(name))
    return action, reason, dispatch, triviality(dispatch)


def main(argv=None):
    mode = os.environ.get("LEOS_AGENT_DISPATCH_GUARD", "on").strip().lower()
    if mode == "off":
        return 0

    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        event = json.loads(raw) if raw.strip() else None
    except Exception:
        return 0

    name = harness(event if isinstance(event, dict) else {})
    try:
        action, reason, dispatch, trivial = evaluate(event)
    except Exception as exc:
        # The guard broke. Allow, and say so distinctly -- an "error" row is the
        # only way a dead guard is ever noticed.
        _breadcrumb(name, exc)
        return 0

    if dispatch is None:
        return 0

    session = _first_str(event, ("session_id", "sessionId")) if isinstance(event, dict) else ""
    cwd = _first_str(event, ("cwd", "workspace", "directory")) if isinstance(event, dict) else ""

    try:
        import dispatch_log
        entry = dispatch_log.record(dispatch, action, reason, name, session, cwd, trivial)
        _log(entry)
    except Exception:
        pass

    if action == BLOCK and mode != "warn":
        sys.stderr.write(render_block(dispatch, name) + "\n")
        return 2

    # A notice is user-facing only. systemMessage reaches Leo's transcript and
    # never the model's context, so a false positive here costs zero tokens.
    # verbose additionally spends ~60 tokens putting it in front of the model;
    # it stays off until the log says the problem is frequent enough to be worth
    # paying for.
    if trivial >= 2:
        payload = {"systemMessage": render_notice(dispatch)}
        if mode == "verbose":
            payload["hookSpecificOutput"] = {
                "hookEventName": "PreToolUse",
                "additionalContext": render_notice(dispatch),
            }
        sys.stdout.write(json.dumps(payload) + "\n")
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
