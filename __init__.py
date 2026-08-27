"""Hermes plugin entry point for leos-agent.

Registers the bundled install skill and a /leo-install command. Everything else in
this repo is consumed by other harnesses through their own manifests; Hermes
only needs the skill and a way to run the installer.
"""

import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
HARNESS = "hermes"


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


def register(ctx):
	ctx.register_skill(PLUGIN_ROOT / "skills" / "install")

	def leo_install(args=""):
		"""Install or update Leo's preferences in ~/.hermes/SOUL.md."""
		flags = [flag for flag in args.split() if flag.startswith("--")]
		return _run_install(*flags)

	ctx.register_command(
		"leo-install",
		leo_install,
		description="Install Leo's global agent preferences into ~/.hermes/SOUL.md (--dry-run, --uninstall).",
	)
