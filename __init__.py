"""Hermes plugin: deferred skills, one policy section, and native cost checks."""
import collections
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

PLUGIN_ROOT = Path(__file__).resolve().parent
HARNESS = "hermes"
_PARENTS = collections.OrderedDict()
_PARENT_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def _python(script, args=(), event=None, timeout=10):
    # Isolate common module names such as state/routing from Hermes's Python
    # process. The same CLI contract is used by the JavaScript adapters.
    return subprocess.run([sys.executable, str(PLUGIN_ROOT / "scripts" / script), *args],
                          input=json.dumps(event or {}), capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, "LEOS_AGENT_HARNESS": HARNESS,
                                                "LEOS_AGENT_ROOT": str(PLUGIN_ROOT)})


def _run_install(*args):
    result = _python("leo-install.py", (HARNESS, *args), timeout=60)
    return (result.stdout + ("\n" + result.stderr if result.stderr.strip() else "")).strip()


def _payload_section(info=None):
    """Hermes freezes this section per session; no dynamic per-turn injection."""
    try:
        result = _python("emit_payload.py")
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    logger.warning("leos-agent policy bridge failed; inspect plugin diagnostics")
    return ""


def _on_request_model(model=None, response_model=None, task_id=None, session_id=None, **_):
    """Observe identifiers only; do not retain provider payloads or secrets."""
    key, model = task_id or session_id, response_model or model
    if not isinstance(key, str) or not isinstance(model, str):
        return
    with _PARENT_LOCK:
        _PARENTS[key] = (model, time.time())
        _PARENTS.move_to_end(key)
        while len(_PARENTS) > 1024:
            _PARENTS.popitem(last=False)


def _on_session_start(**_):
    if os.environ.get("LEOS_AGENT_PRICE_REFRESH") == "off":
        return
    try:
        subprocess.Popen([sys.executable, str(PLUGIN_ROOT / "scripts/pricing.py"), "refresh"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        logger.warning("leos-agent price refresh could not start")


def _native_delegation_model():
    # Hermes's own profile-aware loader; never resolve credentials or contact
    # providers to inspect a model identifier.
    from tools.delegate_tool_config import _load_config
    config = _load_config()
    return config.get("model") if isinstance(config.get("model"), str) else None


def _on_pre_tool_call(tool_name="", args=None, task_id=None, session_id=None, tool_call_id=None, **_):
    if tool_name != "delegate_task":
        return None
    try:
        with _PARENT_LOCK:
            observed = _PARENTS.get(task_id or session_id)
        parent = observed[0] if observed and time.time() - observed[1] < 86400 else None
        try:
            effective = _native_delegation_model()
        except (ImportError, AttributeError):
            logger.warning("leos-agent cannot inspect Hermes delegation configuration")
            effective = None
        response = _python("dispatch_guard.py", ("--json",), {
            "tool_name": tool_name, "tool_input": args, "session_id": session_id or task_id or "",
            "call_id": tool_call_id, "parent_model": parent, "effective_model": effective,
            "cwd": str(Path.cwd()),
        })
        if response.returncode:
            raise ValueError("guard bridge exited unsuccessfully")
        result = json.loads(response.stdout)
        if result["action"] == "block":
            return {"action": "block", "message": "[leo routing] " + result["reason"] + ". " + result.get("retry", "")}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        logger.warning("leos-agent dispatch bridge failed open")
    return None


def register(ctx):
    for skill in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")):
        ctx.register_skill("leo-" + skill.parent.name, skill, description="Leo's " + skill.parent.name + " workflow")
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_api_request", _on_request_model)
    ctx.register_hook("post_api_request", _on_request_model)
    ctx.register_hook("on_session_start", _on_session_start)
    section = getattr(ctx, "register_system_prompt_section", None)
    if section is not None:
        section("leos-agent", _payload_section, position="after_memory", max_chars=4000)
    else:
        logger.warning("leos-agent requires a Hermes version supporting system prompt sections")

    def leo_install(args=""):
        flags = [flag for flag in args.split() if flag.startswith("--")]
        return _run_install(*flags)
    ctx.register_command("leo-install", leo_install, description="Install, check, or remove Leo's Hermes integration.")
