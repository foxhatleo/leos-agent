"""Hermes entrypoint for the Leo plugin."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PAYLOAD = ROOT / "plugins" / "leo"
POLICY_LIMIT = 10_000
_GUARD = None


def _strip_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip()
    return text


def _policy_context():
    policy = _strip_frontmatter(
        (PAYLOAD / "skills" / "using-leo" / "SKILL.md").read_text(encoding="utf-8")
    ).replace("${CLAUDE_PLUGIN_ROOT}", str(PAYLOAD)).rstrip()
    mapping = (PAYLOAD / "skills" / "using-leo" / "references" / "hermes-mapping.md").read_text(
        encoding="utf-8"
    ).rstrip()
    context = f"<leo-policy>\n{policy}\n\n{mapping}\n</leo-policy>"
    if len(context) > POLICY_LIMIT:
        raise ValueError(f"Leo policy exceeds Hermes context limit: {len(context)}")
    return context


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
    return {"context": _policy_context()}


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


def register(ctx):
    for skill_md in sorted((PAYLOAD / "skills").glob("*/SKILL.md")):
        ctx.register_skill(skill_md.parent.name, skill_md)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
