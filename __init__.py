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
_GUARD = []


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


def _guard():
	"""The shared dispatch guard, loaded by path.

	scripts/ is not an importable package from here, and Hermes runs in-process,
	so this is the one way to share exactly the logic the command hooks run.
	Memoised: this runs before every tool call, and re-executing the module each
	time would put a file read on the hot path of an in-process harness.
	"""
	if not _GUARD:
		spec = importlib.util.spec_from_file_location(
			"leos_dispatch_guard", PLUGIN_ROOT / "scripts" / "dispatch_guard.py"
		)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		_GUARD.append(module)
	return _GUARD[0]


def _on_pre_tool_call(tool_name="", args=None, **_):
	"""Refuse a subagent dispatch that names no model. Fails open.

	In-process, so there is no spawn to prefilter against -- normalize() rejects a
	non-dispatch in microseconds. **_ absorbs whatever else Hermes passes: the
	signature is the one piece of this adapter no version of the repo has verified
	against a live session, and a TypeError here would break every tool call.
	"""
	try:
		guard = _guard()
		action, _reason, dispatch, _trivial = guard.evaluate(
			{"tool_name": tool_name, "tool_input": args, "cwd": str(Path.cwd())}, HARNESS
		)
	except Exception:
		return None
	if action != guard.BLOCK or dispatch is None:
		return None
	return {"action": "block", "message": guard.render_block(dispatch, HARNESS)}


def register(ctx):
	ctx.register_skill(PLUGIN_ROOT / "skills" / "install")
	ctx.register_hook("pre_tool_call", _on_pre_tool_call)

	def leo_install(args=""):
		"""Install or update Leo's preferences in ~/.hermes/SOUL.md."""
		flags = [flag for flag in args.split() if flag.startswith("--")]
		return _run_install(*flags)

	ctx.register_command(
		"leo-install",
		leo_install,
		description="Install Leo's global agent preferences into ~/.hermes/SOUL.md (--dry-run, --uninstall).",
	)
