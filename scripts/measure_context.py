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
	"global_policy_bytes": 2_600,
	# Default policy body, with native Cursor loading accounted separately.
	"rendered_policy_bytes": 2_600,
	"codex_implicit_skill_metadata_bytes": 600,
	"codex_discoverable_skill_metadata_bytes": 2_000,
	"claude_implicit_skill_metadata_bytes": 800,
	"codex_agent_description_bytes": 550,
	# The roster line Claude Code builds per agent: namespaced type, description,
	# and its "(Tools: ...)" list.
	"claude_agent_description_bytes": 800,
	# Command descriptions are listed alongside skills in Claude Code and Cursor,
	# so they are always-loaded context on the same terms as skill metadata.
	"command_description_bytes": 0,
	"review_dispatch_bytes": 1_800,
	# Deferred, but every review reads it: the reviewer worker's brief names it.
	"review_procedure_bytes": 20_000,
	# Deferred and conditional: read only before staging a blocking finding, and
	# only when the repository has recorded review learnings.
	"review_validation_bytes": 2_600,
	"review_learnings_bytes": 2_200,
	# Read by the parent once a review is staged, to show it and ask; and by a
	# watcher's follow-up round, or a reviewer judging pushback on a thread.
	"review_submit_bytes": 3_000,
	"review_followup_bytes": 2_800,
	# Claude-only skill bodies, one budget each; a new skill there needs one.
	"attach_pr_body_bytes": 11_200,
	"watch_review_body_bytes": 9_600,
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


def claude_agent_listing(path, plugin):
	"""One agent's entry in Claude Code's Agent-tool roster, approximately as it
	renders it: `<plugin>:<name>: <description> (Tools: <tools>)`. The tool list
	is the `tools` field minus `disallowedTools`, or all tools when unrestricted."""
	fm, _ = frontmatter(path)
	split = lambda value: [t.strip() for t in value.split(",") if t.strip()]
	denied = split(field(fm, "disallowedTools"))
	tools = split(field(fm, "tools"))
	if tools:
		shown = ", ".join(t for t in tools if t not in denied)
	elif denied:
		shown = "All tools except " + ", ".join(denied)
	else:
		shown = "All tools"
	return f"{plugin}:{field(fm, 'name')}: {field(fm, 'description')} (Tools: {shown})"


def body_bytes(path):
	text = path.read_text(encoding="utf-8")
	return byte_len(frontmatter(path)[1].strip() if text.startswith("---\n") else text.strip())


def rendered_policy():
	"""Default rendered bodies; Cursor instead loads the native rule body.
	Harness wrappers and listing formats are outside this byte measurement.
	"""
	spec = importlib.util.spec_from_file_location("leo_install_measure", ROOT / "scripts" / "leo-install.py")
	installer = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(installer)
	sizes = {h: byte_len(installer.payload_body(ROOT, h, {})) for h in installer.HARNESSES}
	_, raw = frontmatter(ROOT / "rules/preferences.md")
	sizes["cursor"] = byte_len(raw.strip())  # Cursor reads the native rule directly
	return sizes


def measurements():
	portable = sorted((ROOT / "skills").glob("*/SKILL.md"))
	claude_only = sorted((ROOT / "skills-claude").glob("*/SKILL.md"))
	policy_fm, policy_body = frontmatter(ROOT / "rules" / "preferences.md")
	del policy_fm
	review_fm, review_body = frontmatter(ROOT / "skills" / "review-pr" / "SKILL.md")
	del review_fm
	agent_paths = sorted((ROOT / "payload" / "codex-agents").glob("*.toml"))
	# Claude Code lists every plugin agent in the parent's always-loaded agent
	# roster, so each entry is part of the static footprint too.
	plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
	claude_agent_bytes = sum(byte_len(claude_agent_listing(path, plugin))
		for path in sorted((ROOT / "agents").glob("*.md")))
	command_paths = sorted((ROOT / "commands").glob("*.md")) + sorted((ROOT / "commands-claude").glob("*.md"))
	command_bytes = 0
	for path in command_paths:
		fm, _ = frontmatter(path)
		command_bytes += byte_len(field(fm, "description"))
	claude_bodies = {f"{path.parent.name.replace('-', '_')}_body_bytes": body_bytes(path) for path in claude_only}
	return {
		"global_policy_bytes": byte_len(policy_body.strip()),
		"rendered_policy_bytes": max(rendered_policy().values()),
		"codex_implicit_skill_metadata_bytes": skill_metadata_bytes(portable, codex_implicit),
		"codex_discoverable_skill_metadata_bytes": skill_metadata_bytes(portable, lambda *_: True),
		"claude_implicit_skill_metadata_bytes": skill_metadata_bytes(portable + claude_only, claude_implicit),
		"codex_agent_description_bytes": sum(byte_len(agent_description(path)) for path in agent_paths),
		"claude_agent_description_bytes": claude_agent_bytes,
		"command_description_bytes": command_bytes,
		"review_dispatch_bytes": byte_len(review_body.strip()),
		"review_procedure_bytes": body_bytes(ROOT / "skills" / "review-pr" / "reference" / "procedure.md"),
		"review_validation_bytes": body_bytes(ROOT / "skills" / "review-pr" / "reference" / "validation.md"),
		"review_learnings_bytes": body_bytes(ROOT / "skills" / "review-pr" / "reference" / "learnings.md"),
		"review_submit_bytes": body_bytes(ROOT / "skills" / "review-pr" / "reference" / "submit.md"),
		"review_followup_bytes": body_bytes(ROOT / "skills" / "review-pr" / "reference" / "followup.md"),
		**claude_bodies,
	}


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
	parser.add_argument("--check", action="store_true", help="fail when a committed ceiling is exceeded")
	args = parser.parse_args(argv)

	values = measurements()
	# A measurement without a ceiling (a new Claude-only skill) fails --check
	# rather than going unbudgeted.
	unbudgeted = sorted(set(values) - set(LIMITS))
	if args.json:
		print(json.dumps({"measurements": values, "limits": LIMITS}, indent=2, sort_keys=True))
	else:
		print("Static prompt text (UTF-8 bytes; byte/4 is only a rough prose-token proxy)")
		for name, value in values.items():
			limit = f"{LIMITS[name]:5}" if name in LIMITS else " none"
			print(f"  {name:38} {value:5}  limit {limit}")
		print("  default policy body by harness (Cursor uses its native rule):")
		for harness, value in sorted(rendered_policy().items()):
			print(f"    {harness:38} {value:5}")
		print("Codex implicit-invocation policy does not establish hidden metadata: both eligible and all discoverable names/descriptions are counted.")
		print("Excludes harness wrappers, file-path metadata, conversation, tools, cache effects, and child work; these are component budgets, not total injected tokens.")

	over = {name: (value, LIMITS[name]) for name, value in values.items() if name in LIMITS and value > LIMITS[name]}
	if args.check and (over or unbudgeted):
		for name, (value, limit) in over.items():
			print(f"FAIL {name}: {value} > {limit}", file=sys.stderr)
		for name in unbudgeted:
			print(f"FAIL {name}: no ceiling in LIMITS", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())
