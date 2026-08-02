"""Hermes entrypoint for the Leo plugin."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import threading


ROOT = Path(__file__).resolve().parent
PAYLOAD = ROOT / "plugins" / "leo"
# Self-imposed context budget, not a documented Hermes API cap: it exists so
# the policy can't grow without someone noticing. Hermes also carries the
# generated harness mapping and optional memory envelope, so its ceiling is
# slightly larger than Claude's SessionStart budget. Overflow degrades to no
# injection (see _policy_context).
#
# Deliberate raise from 16000 after adding the 7.0 policy and role metadata.
# The memory index is appended after the policy and is bounded by whatever is
# left under this ceiling, so the number now covers policy + mapping + memory
# rather than policy + mapping alone. _render_policy() stays policy-only and
# keeps its own growth headroom check; _context() is what this limit bounds.
POLICY_LIMIT = 17_000
_GUARD = None


def _breadcrumb(exc):
    """Record why injection was skipped. Never raises: this is the fail path."""
    try:
        base = os.environ.get("LEOS_AGENT_LOCAL_PATH") or str(Path.home() / ".leos-agent-local")
        local = Path(base)
        local.mkdir(parents=True, exist_ok=True)
        with (local / "hermes-policy.log").open("a", encoding="utf-8") as fh:
            fh.write(f"policy injection skipped: {type(exc).__name__}: {exc}\n")
    except Exception:
        pass


def _strip_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip()
    return text


def _render_policy():
    """Build the policy context. Raises if it exceeds the Hermes limit."""
    policy = _strip_frontmatter(
        (PAYLOAD / "skills" / "using-leo" / "SKILL.md").read_text(encoding="utf-8")
    ).rstrip()
    mapping = (PAYLOAD / "skills" / "using-leo" / "references" / "hermes-mapping.md").read_text(
        encoding="utf-8"
    ).rstrip()
    # Substitute AFTER the append so placeholders inside the mapping resolve
    # too — same ordering as hooks/session-start.py.
    body = f"{policy}\n\n{mapping}".replace("${CLAUDE_PLUGIN_ROOT}", str(PAYLOAD))
    context = f"<leo-policy>\n{body}\n</leo-policy>"
    if len(context) > POLICY_LIMIT:
        raise ValueError(f"Leo policy exceeds Hermes context limit: {len(context)}")
    return context


def _memory():
    """Load scripts/memory.py the same way the guard is loaded, at :73-81."""
    spec = importlib.util.spec_from_file_location(
        "leo_memory", str(PAYLOAD / "scripts" / "memory.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _memory_block():
    """The memory index, or "" on any failure. Never raises."""
    try:
        return _memory().session(os.getcwd())
    except Exception as exc:
        _breadcrumb(exc)
        return ""


def _context():
    """Policy plus memory, bounded as a whole.

    _render_policy stays policy-only on purpose: it carries the growth-headroom
    gate, and folding a store that grows on its own into that measurement would
    turn a tripwire for policy creep into noise. Memory is appended here and
    trimmed to whatever room is left, so it can never be the reason the policy
    stops being injected.
    """
    context = _render_policy()
    block = _memory_block()
    if not block:
        return context
    envelope = f"\n\n<leo-memory>\n{block.rstrip()}\n</leo-memory>"
    if len(context) + len(envelope) > POLICY_LIMIT:
        return context
    return context + envelope


def _policy_context():
    """Fail open: a policy that outgrew the budget must not break every turn.

    This runs inside pre_llm_call, so raising here would take down the whole
    session for a context-injection convenience — the same trade
    hooks/session-start.py already refuses to make. Degrade to no policy and
    leave a breadcrumb instead.
    """
    try:
        return _context()
    except Exception as exc:
        _breadcrumb(exc)
        return None


def _load_guard():
    global _GUARD
    if _GUARD is None:
        path = PAYLOAD / "hooks" / "bash-guard.py"
        spec = importlib.util.spec_from_file_location("leo_bash_guard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GUARD = module
    return _GUARD


_INJECTED = set()
_INJECTED_UNKEYED = False
_PRIMARY_ALIVE = False
_INJECTION_LOCK = threading.Lock()


def _claim_injection(task_id):
    """Test-and-set for the fallback channel: once per session, and never at
    all once the primary channel has proved it works.

    Hermes passes task_id as the session identifier, or an empty string when
    it is unset. Keyed when present; a single process-wide flag when not,
    which is exact for the CLI (one process, one session) and deliberately
    coarse rather than a guess anywhere else.

    The asymmetry is the point. pre_llm_call's contract is to supply context
    on every call, so gating *it* to once per session would make the policy
    vanish after turn one the day upstream wires it up — trading a silent
    absence for a subtler one. Instead the primary channel stays unbounded and
    the fallback stands down as soon as it fires.
    """
    global _INJECTED_UNKEYED
    key = (
        hashlib.sha256(task_id.encode("utf-8")).hexdigest()
        if isinstance(task_id, str) and task_id
        else None
    )
    with _INJECTION_LOCK:
        if _PRIMARY_ALIVE:
            return False
        if key is None:
            if _INJECTED_UNKEYED:
                return False
            _INJECTED_UNKEYED = True
            return True
        if key in _INJECTED:
            return False
        # Session identities are process-lifetime, deliberately never evicted:
        # evicting an old key causes a duplicate policy injection when a
        # long-lived gateway returns to that session.
        _INJECTED.add(key)
        return True


def _on_pre_llm_call(**_):
    """Registered but never invoked upstream (#2817). Left intact and
    unbounded so that the day it starts firing it simply works, and marks
    itself alive so the tool-result fallback stops duplicating it."""
    context = _policy_context()
    if context is None:
        return None
    global _PRIMARY_ALIVE
    with _INJECTION_LOCK:
        _PRIMARY_ALIVE = True
    return {"context": context}


def _on_transform_tool_result(result="", task_id=None, **_):
    """Ride the session's first tool result.

    pre_llm_call is registered and never invoked (#2817, closed as not
    planned), so for two years the policy has reached a Hermes turn only if
    the user read leo:using-leo by hand. pre_tool_call *is* invoked, but the
    only thing it can return that reaches the model is a block directive —
    injecting through it would mean denying the user's tool call and dressing
    the policy up as an error, which is worse than not injecting at all.
    transform_tool_result is the one invoked hook that can put text into the
    conversation without refusing anything.

    Appended, never substituted: the model still gets its tool output. Any
    failure returns None, which the runtime reads as "leave the result
    alone" — a bug here would destroy tool output, so every path that is not
    a confident success returns None.
    """
    if not isinstance(result, str):
        return None
    try:
        context = _context()
    except Exception as exc:
        _breadcrumb(exc)
        return None
    if not context:
        return None
    # Build first. A broken memory/policy read must leave this session eligible
    # to retry on its next tool result instead of consuming its one fallback.
    if not _claim_injection(task_id):
        return None
    return f"{result}\n\n{context}"


def _on_pre_tool_call(tool_name="", args=None, **_):
    if tool_name not in {"terminal", "bash", "shell", "execute_command"}:
        return None
    args = args if isinstance(args, dict) else {}
    command = args.get("command", args.get("cmd"))
    if not isinstance(command, str) or not command:
        return None
    cwd = args.get("cwd") if isinstance(args.get("cwd"), str) else None
    try:
        reason = _load_guard().check(command, cwd)
    except Exception:
        reason = "internal error while evaluating the command; failing closed"
    if not reason:
        return None
    return {
        "action": "block",
        "message": (
            f"[bash-guard] BLOCKED — {reason}. This is an accidental catastrophic-command "
            "tripwire, not a general or adversarial shell-security boundary."
        ),
    }


def _excluded_skills(harness="hermes"):
    """Skill dirs this harness must not register.

    The Claude-only skills already live outside skills/, so the glob below
    never sees them. Nothing else is excluded on Hermes today: using-leo used
    to be, on the assumption its body arrived as context every turn — see the
    register() note for why that assumption was wrong and costly.
    """
    config = json.loads((PAYLOAD / "config" / "models.json").read_text(encoding="utf-8"))
    return set(config.get("skills", {}).get("exclude", {}).get(harness, ()))


def register(ctx):
    # Hermes runs no session-start hook and exports no plugin-root variable, so
    # scripts/doctor.py has no way to tell this harness apart from a bare shell
    # and used to inherit the bootstrap's "claude" default. Declare it.
    os.environ["LEOS_AGENT_HARNESS"] = "hermes"
    # Hermes accepts pre_llm_call in register_hook() and lists it in VALID_HOOKS,
    # but its runtime never calls invoke_hook() with it (upstream issue #2817,
    # closed as not planned). So _on_pre_llm_call has never run, and the policy
    # never reached a Hermes turn — silently, because a hook that is never
    # invoked cannot even leave a breadcrumb. transform_tool_result carries it
    # instead; the pre_llm_call registration stays so injection improves the day
    # upstream wires it up, and both share one gate so it cannot double-inject.
    # Registering using-leo as a normal skill also stays: a session that runs no
    # tool at all still has to be able to load the policy by reading it.
    excluded = _excluded_skills("hermes")
    for skill_md in sorted((PAYLOAD / "skills").glob("*/SKILL.md")):
        if skill_md.parent.name in excluded:
            continue
        ctx.register_skill(skill_md.parent.name, skill_md)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    # register() is the only Hermes code path that reliably runs at session
    # start, so the memory refresh hangs off it. Hermes has no memory surface
    # of its own, but the refresh keeps the store's index current and projects
    # the global facts to the harnesses that do.
    _memory_block()
