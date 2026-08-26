"""Hermes entrypoint for the Leo plugin.

Hermes installs the Git repository into its plugin directory and loads this
module, so the repository root is the plugin root here and `plugins/leo` is the
payload beneath it. The dependency points inward only: nothing in the payload
reaches back out to these two files.

What this registers is deliberately narrow. Leo's policy reaches the model the
same way it does on every other harness in 8.0 and later -- as ordinary skills
the harness loads natively, through `ctx.register_skill`. It is never injected.
7.x did inject it, appending the policy to the session's first tool result via
`transform_tool_result` because Hermes accepts `pre_llm_call` in
`register_hook()` but never invokes it (upstream #2817, closed as not planned).
That cost every session a multi-thousand-token block it had not asked for, and
it is why the harness was dropped in 8.0 rather than kept. Do not add it back:
if the policy needs to reach a session, the session reads `leo:routing`.
"""

import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PAYLOAD = ROOT / "plugins" / "leo"

_GUARD = None


def _load_guard():
    """Load hooks/bash-guard.py once, by path.

    The payload is not an importable package -- it is a plugin tree copied
    around by five different harnesses -- so the guard is loaded by file
    location rather than imported. Detection lives there and only there;
    `hooks/cursor-guard.py` is the other adapter over the same `check()`.
    """
    global _GUARD
    if _GUARD is None:
        path = PAYLOAD / "hooks" / "bash-guard.py"
        spec = importlib.util.spec_from_file_location("leo_bash_guard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GUARD = module
    return _GUARD


def _on_pre_tool_call(tool_name="", args=None, **_):
    """The catastrophic-command tripwire, in Hermes's block-directive shape.

    Fails closed: if evaluating the command raises, the command is blocked. A
    guard that fails open is not a guard, and the cost of a false block here is
    one retry.
    """
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


def _excluded_skills():
    """Skill directories this harness must not register.

    Read from the canonical matrix rather than restated here, so an exclusion is
    declared in exactly one place. The Claude-only skills live outside `skills/`
    and so are never seen by the glob in register().
    """
    config = json.loads((PAYLOAD / "config" / "models.json").read_text(encoding="utf-8"))
    return set(config.get("skills", {}).get("exclude", {}).get("hermes", ()))


def register(ctx):
    # Hermes exports no plugin-root variable, so a session has nothing to detect
    # this harness by. Declare it: skills/routing/references/harnesses.md tells
    # the model to read LEOS_AGENT_HARNESS first for exactly this reason.
    os.environ["LEOS_AGENT_HARNESS"] = "hermes"
    excluded = _excluded_skills()
    for skill_md in sorted((PAYLOAD / "skills").glob("*/SKILL.md")):
        if skill_md.parent.name in excluded:
            continue
        ctx.register_skill(skill_md.parent.name, skill_md)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
