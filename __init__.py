"""Hermes plugin entry point for leos-agent.

Registers the bundled install skill and a /leo-install command. Everything else in
this repo is consumed by other harnesses through their own manifests; Hermes
only needs the skill and a way to run the installer.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
HARNESS = "hermes"
_MODULES = {}


def _run_install(*args):
	"""Run the installer for Hermes only and return its combined output."""
	script = PLUGIN_ROOT / "scripts" / "leo-install.py"
	result = subprocess.run(
		[sys.executable, str(script), HARNESS, *args],
		capture_output=True,
		text=True,
		timeout=60,
	)
	output = result.stdout + (f"\n{result.stderr}" if result.stderr.strip() else "")
	return output.strip()


def _load(name):
	"""Import scripts/<name>.py by path, memoized per process.

	scripts/ is not an importable package from here, and Hermes runs in-process,
	so this is the one way to share exactly the logic the command hooks and the
	system-prompt section below run. Memoised: the guard runs before every tool
	call and the payload loader runs once per session, and re-executing a module
	from disk on either path would put a file read on the hot path of an
	in-process harness.
	"""
	if name not in _MODULES:
		spec = importlib.util.spec_from_file_location(f"leos_{name}", PLUGIN_ROOT / "scripts" / f"{name}.py")
		module = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = module
		spec.loader.exec_module(module)
		_MODULES[name] = module
	return _MODULES[name]


def _guard():
	"""The shared dispatch guard, loaded by path. See _load() for why."""
	return _load("dispatch_guard")


def _payload_section(info=None):
	"""The rendered policy payload, for register_system_prompt_section.

	This is the cache-safe path: Hermes renders a system-prompt section once per
	session and freezes it, unlike pre_llm_call, which re-renders on every turn
	and would repeatedly invalidate the prompt prefix it sits in. Must fail open
	-- an exception raised from here, rather than an empty string returned,
	would take the section (and possibly the session) down with it.
	"""
	try:
		payload = _load("payload")
		routing = _load("routing")
		return payload.payload_body(PLUGIN_ROOT, HARNESS, routing.load())
	except Exception:
		return ""


def _on_pre_tool_call(tool_name="", args=None, task_id=None, **_):
	"""Refuse a subagent dispatch that names no model. Fails open.

	In-process, so there is no spawn to prefilter against -- normalize() rejects a
	non-dispatch in microseconds. **_ absorbs whatever else Hermes passes: the
	signature is the one piece of this adapter no version of the repo has verified
	against a live session, and a TypeError here would break every tool call.
	"""
	try:
		guard = _guard()
		result = guard.process(
			{"tool_name": tool_name, "tool_input": args, "session_id": task_id or "", "cwd": str(Path.cwd())}, HARNESS
		)
	except Exception:
		return None
	if result["action"] == "block":
		return {"action": "block", "message": guard.render_block(None, HARNESS, result)}
	return None



def register(ctx):
	for skill in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")):
		ctx.register_skill("leo-" + skill.parent.name, skill, description="Leo's " + skill.parent.name + " workflow")
	ctx.register_hook("pre_tool_call", _on_pre_tool_call)

	# Older Hermes builds have no such API; degrade silently rather than break
	# register() over a section the running version cannot render.
	register_section = getattr(ctx, "register_system_prompt_section", None)
	if register_section is not None:
		register_section("leos-agent", _payload_section, position="after_memory", max_chars=4000)

	def leo_install(args=""):
		"""Run Leo's installer for Hermes (--dry-run, --uninstall)."""
		flags = [flag for flag in args.split() if flag.startswith("--")]
		return _run_install(*flags)

	ctx.register_command(
		"leo-install",
		leo_install,
		description="Run Leo's installer for Hermes (--dry-run, --uninstall).",
	)
