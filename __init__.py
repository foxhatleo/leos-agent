"""Hermes entrypoint for the Leo plugin."""

import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PAYLOAD = ROOT / "plugins" / "leo"
# Self-imposed context budget, not a documented Hermes API cap: it exists so
# the policy can't grow without someone noticing. 14000 matches the Claude
# SessionStart budget in tests/test_session_start.py, so both bootstraps are
# held to one number. Overflow degrades to no injection (see _policy_context).
POLICY_LIMIT = 14_000
_GUARD = None


def _breadcrumb(exc):
    """Record why injection was skipped. Never raises: this is the fail path."""
    try:
        base = os.environ.get("LEOS_AGENT_PATH") or str(Path.home() / ".leos-agent")
        local = Path(base) / "local"
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


def _policy_context():
    """Fail open: a policy that outgrew the budget must not break every turn.

    This runs inside pre_llm_call, so raising here would take down the whole
    session for a context-injection convenience — the same trade
    hooks/session-start.py already refuses to make. Degrade to no policy and
    leave a breadcrumb instead.
    """
    try:
        return _render_policy()
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


def _on_pre_llm_call(**_):
    context = _policy_context()
    if context is None:
        return None
    return {"context": context}


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
    # Hermes accepts pre_llm_call in register_hook() and lists it in VALID_HOOKS,
    # but its runtime never calls invoke_hook() with it (upstream issue #2817,
    # closed as not planned). So _on_pre_llm_call below has never run, and the
    # policy has never reached a Hermes turn — silently, because a hook that is
    # never invoked cannot even leave a breadcrumb. Registering using-leo as a
    # normal skill is the part that actually works today; the hook stays
    # registered so injection starts working the day upstream wires it up.
    excluded = _excluded_skills("hermes")
    for skill_md in sorted((PAYLOAD / "skills").glob("*/SKILL.md")):
        if skill_md.parent.name in excluded:
            continue
        ctx.register_skill(skill_md.parent.name, skill_md)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
