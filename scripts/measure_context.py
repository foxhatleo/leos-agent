#!/usr/bin/env python3
"""Measure leos-agent's static prompt footprint with a byte-based proxy.

This does not estimate total task cost: tool output, conversation history,
cache state, model choice, and spawned work dominate many real runs. It measures
the repository-controlled text that is always listed or loaded at dispatch, so
regressions remain visible without a tokenizer or network access.
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Deliberately tight ceilings. Raise one only with a concrete reason and record
# the before/after output in the change that raises it.
LIMITS = {
	"global_policy_bytes": 4_500,
	# What an unconfigured machine actually installs. Rendering the routing region
	# per harness dropped this below the old whole-file figure of 4497, and it must
	# stay there: the model config exists to save money, so it may not cost
	# always-loaded bytes to have. A configured harness exceeds this only by the
	# length of the model names chosen, which is bounded and deliberate.
	"rendered_policy_bytes": 4_497,
	"codex_implicit_skill_metadata_bytes": 600,
	"claude_implicit_skill_metadata_bytes": 800,
	"codex_agent_description_bytes": 550,
	"claude_agent_description_bytes": 550,
	"review_dispatch_bytes": 3_500,
}


def frontmatter(path):
	text = path.read_text(encoding="utf-8")
	match = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
	if not match:
		raise ValueError(f"{path.relative_to(ROOT)} has no YAML frontmatter")
	return match.group(1), match.group(2)


def field(text, name):
	match = re.search(rf"(?m)^{re.escape(name)}:\s*(.+?)\s*$", text)
	return match.group(1).strip() if match else ""


def byte_len(text):
	return len(text.encode("utf-8"))


def skill_metadata_bytes(paths, implicit):
	total = 0
	for path in paths:
		fm, _ = frontmatter(path)
		if implicit(path, fm):
			total += byte_len(field(fm, "name")) + byte_len(field(fm, "description"))
	return total


def codex_implicit(path, _frontmatter):
	policy = path.parent / "agents" / "openai.yaml"
	return not policy.is_file() or "allow_implicit_invocation: false" not in policy.read_text(encoding="utf-8")


def claude_implicit(_path, fm):
	return re.search(r"(?m)^disable-model-invocation:\s*true\s*$", fm) is None


def agent_description(path):
	text = path.read_text(encoding="utf-8")
	match = re.search(r'(?m)^description\s*=\s*"(.*)"\s*$', text)
	if not match:
		raise ValueError(f"{path.relative_to(ROOT)} has no one-line description")
	return match.group(1)


def rendered_policy():
	"""The installed payload body per harness, with no routing config present.

	This is what a session actually loads -- rules/preferences.md on disk keeps a
	harness-neutral default in its routing region, and the installer narrows it to
	one harness. Measured with the config forced empty so the number is a property
	of the repository, not of whoever runs it.
	"""
	spec = importlib.util.spec_from_file_location("leo_install_measure", ROOT / "scripts" / "leo-install.py")
	installer = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(installer)
	return {h: byte_len(installer.payload_body(ROOT, h, {})) for h in installer.HARNESSES}


def measurements():
	portable = sorted((ROOT / "skills").glob("*/SKILL.md"))
	claude_only = sorted((ROOT / "skills-claude").glob("*/SKILL.md"))
	policy_fm, policy_body = frontmatter(ROOT / "rules" / "preferences.md")
	del policy_fm
	review_fm, review_body = frontmatter(ROOT / "skills" / "review-pr" / "SKILL.md")
	del review_fm
	agent_paths = sorted((ROOT / "payload" / "codex-agents").glob("*.toml"))
	# Claude Code lists every plugin agent's name and description in the parent's
	# always-loaded agent roster, so they are part of the static footprint too.
	claude_agent_paths = sorted((ROOT / "agents").glob("*.md"))
	claude_agent_bytes = 0
	for path in claude_agent_paths:
		fm, _ = frontmatter(path)
		claude_agent_bytes += byte_len(field(fm, "name")) + byte_len(field(fm, "description"))
	return {
		"global_policy_bytes": byte_len(policy_body.strip()),
		"rendered_policy_bytes": max(rendered_policy().values()),
		"codex_implicit_skill_metadata_bytes": skill_metadata_bytes(portable, codex_implicit),
		"claude_implicit_skill_metadata_bytes": skill_metadata_bytes(portable + claude_only, claude_implicit),
		"codex_agent_description_bytes": sum(byte_len(agent_description(path)) for path in agent_paths),
		"claude_agent_description_bytes": claude_agent_bytes,
		"review_dispatch_bytes": byte_len(review_body.strip()),
	}


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
	parser.add_argument("--check", action="store_true", help="fail when a committed ceiling is exceeded")
	args = parser.parse_args(argv)

	values = measurements()
	if args.json:
		print(json.dumps({"measurements": values, "limits": LIMITS}, indent=2, sort_keys=True))
	else:
		print("Static prompt footprint (bytes; tokens are roughly bytes / 4 for this prose)")
		for name, value in values.items():
			print(f"  {name:38} {value:5}  limit {LIMITS[name]:5}")
		print("  rendered_policy_bytes is the worst case across harnesses; each one installs:")
		for harness, value in sorted(rendered_policy().items()):
			print(f"    {harness:38} {value:5}")
		print("This excludes conversation history, tool output, cache effects, and subagent work.")

	over = {name: (value, LIMITS[name]) for name, value in values.items() if value > LIMITS[name]}
	if args.check and over:
		for name, (value, limit) in over.items():
			print(f"FAIL {name}: {value} > {limit}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())
