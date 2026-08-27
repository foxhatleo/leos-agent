#!/usr/bin/env python3
"""Structural checks for the leos-agent repo. Run in CI and before a release.

Asserts the version is identical across every manifest, that each harness's
manifest carries what that harness requires, and that the injection round-trips
idempotently and refuses to touch files whose markers are malformed. Stdlib only.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "leos-agent"

failures = []
checks = 0


def check(condition, message):
	global checks
	checks += 1
	if not condition:
		failures.append(message)


def raises_block_error(installer, text):
	try:
		installer.inject(text, "<leos-agent version=\"0\">\nx\n</leos-agent>\n")
	except installer.BlockError:
		return True
	except Exception as exc:  # a different failure is still a failed check, not a crash
		failures.append(f"expected BlockError but got {type(exc).__name__}: {exc}")
	return False


def load_installer():
	spec = importlib.util.spec_from_file_location("leo_install", ROOT / "scripts" / "leo-install.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def main():
	installer = load_installer()
	canonical = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
	check(re.fullmatch(r"\d+\.\d+\.\d+", canonical) is not None, f"package.json version {canonical!r} is not strict semver")

	# 1. Every manifest agrees on version and name.
	for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json"):
		data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
		check(data.get("version") == canonical, f"{rel}: version {data.get('version')!r} != {canonical!r}")
		check(data.get("name") == NAME, f"{rel}: name {data.get('name')!r} != {NAME!r}")

	yaml_text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
	yaml_version = re.search(r"^version:\s*['\"]?([^'\"\s]+)", yaml_text, re.MULTILINE)
	check(yaml_version is not None and yaml_version.group(1) == canonical, f"plugin.yaml: version != {canonical!r}")
	yaml_name = re.search(r"^name:\s*['\"]?([^'\"\s]+)", yaml_text, re.MULTILINE)
	check(yaml_name is not None and yaml_name.group(1) == NAME, f"plugin.yaml: name != {NAME!r}")

	# 2. Neither Claude Code nor Codex may declare hooks/hooks.json: both load it
	# automatically, and naming it again is a duplicate. Claude Code fails the
	# whole plugin at load time for this, and its own `plugin validate` does not
	# catch it -- only a real install does.
	claude_manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
	declared = claude_manifest.get("hooks")
	declared = declared if isinstance(declared, list) else [declared] if declared else []
	check(
		not any(str(p).endswith("hooks/hooks.json") for p in declared),
		".claude-plugin/plugin.json: must not declare hooks/hooks.json (auto-loaded; declaring it fails the plugin)",
	)
	codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
	check("hooks" not in codex, ".codex-plugin/plugin.json: must omit `hooks` (validator rejects it; hooks/ is auto-discovered)")
	check(codex.get("description"), ".codex-plugin/plugin.json: description is required")
	check(codex.get("author", {}).get("name"), ".codex-plugin/plugin.json: author.name is required")
	interface = codex.get("interface", {})
	for field in ("displayName", "shortDescription", "longDescription", "developerName", "category"):
		check(interface.get(field), f".codex-plugin/plugin.json: interface.{field} is required")

	# 3. Marketplaces parse, name this plugin, and carry no stale version.
	for rel in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
		data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
		plugins = data.get("plugins", [])
		check(len(plugins) == 1, f"{rel}: expected exactly one plugin entry")
		if plugins:
			entry = plugins[0]
			check(entry.get("name") == NAME, f"{rel}: plugin name != {NAME!r}")
			if rel.startswith(".claude-plugin"):
				check(entry.get("version") == canonical, f"{rel}: plugin version {entry.get('version')!r} != {canonical!r}")

	# Every version string the README hardcodes must be the current one: the
	# uninstall commands point at versioned cache paths.
	readme = (ROOT / "README.md").read_text(encoding="utf-8")
	check(canonical in readme, f"README.md does not mention version {canonical}")
	stale = {v for v in re.findall(r"\b\d+\.\d+\.\d+\b", readme) if v != canonical}
	check(not stale, f"README.md mentions non-current version(s): {sorted(stale)}")

	# 4. The payload is a valid Cursor rule with a real body.
	prefs = (ROOT / "rules" / "preferences.md").read_text(encoding="utf-8")
	check(prefs.startswith("---\n"), "rules/preferences.md: missing YAML frontmatter")
	frontmatter = prefs.split("---", 2)[1] if prefs.count("---") >= 2 else ""
	check("alwaysApply: true" in frontmatter, "rules/preferences.md: frontmatter needs alwaysApply: true")
	check("description:" in frontmatter, "rules/preferences.md: frontmatter needs a description")
	body = installer.payload_body(ROOT)
	check(len(body) > 200, "rules/preferences.md: body is suspiciously short")
	check("<leos-agent" not in body and "</leos-agent>" not in body, "rules/preferences.md: body must not contain a marker")

	# 5. Skills and commands exist and carry the portable frontmatter subset.
	skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
	check(len(skills) >= 1, "skills/: no SKILL.md found; the plugin must ship at least one skill")
	for skill in skills:
		text = skill.read_text(encoding="utf-8")
		check(text.startswith("---\n"), f"{skill.relative_to(ROOT)}: missing frontmatter")
		fm = text.split("---", 2)[1] if text.count("---") >= 2 else ""
		check(re.search(r"^name:", fm, re.MULTILINE) is not None, f"{skill.relative_to(ROOT)}: needs name")
		check(re.search(r"^description:", fm, re.MULTILINE) is not None, f"{skill.relative_to(ROOT)}: needs description")
	commands = sorted((ROOT / "commands").glob("*.md"))
	check(len(commands) >= 1, "commands/: no command files found")

	# 6. Every path a manifest points at must exist, and hook files must parse in
	# their own harness's format. A manifest referencing a missing file ships a
	# broken plugin, so absence has to fail rather than skip.
	for rel, keys in (
		(".claude-plugin/plugin.json", ("skills", "commands", "hooks")),
		(".cursor-plugin/plugin.json", ("rules", "skills", "commands", "hooks")),
		(".codex-plugin/plugin.json", ("skills",)),
	):
		data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
		for key in keys:
			value = data.get(key)
			for declared in (value if isinstance(value, list) else [value] if value else []):
				check((ROOT / declared).exists(), f"{rel}: {key} points at {declared}, which does not exist")

	shared_hooks = ROOT / "hooks" / "hooks.json"
	check(shared_hooks.is_file(), "hooks/hooks.json is missing (Claude Code and Codex read it)")
	if shared_hooks.is_file():
		data = json.loads(shared_hooks.read_text(encoding="utf-8"))
		check(isinstance(data.get("hooks"), dict), "hooks/hooks.json: needs a top-level `hooks` object")
	cursor_hooks = ROOT / "hooks" / "hooks-cursor.json"
	check(cursor_hooks.is_file(), "hooks/hooks-cursor.json is missing (Cursor reads it)")
	if cursor_hooks.is_file():
		data = json.loads(cursor_hooks.read_text(encoding="utf-8"))
		check(data.get("version") == 1, "hooks/hooks-cursor.json: Cursor requires version 1")
		check(isinstance(data.get("hooks"), dict), "hooks/hooks-cursor.json: needs a top-level `hooks` object")

	# Payload files copied by the installer must carry the provenance string, or
	# it will mistake its own installed copy for a stranger's file and refuse to
	# upgrade or remove it. The list is derived from the installer's own copy sets,
	# so a skill added there can never slip past this check.
	copied = ["skills/install/SKILL.md", "commands/leo-install.md"]
	copied.extend(f"payload/codex-agents/{name}.toml" for name in installer.CODEX_AGENTS)
	for name in installer.OPENCODE_SKILLS:
		copied.append(f"skills/{name}/SKILL.md")
		copied.extend(str(p.relative_to(ROOT)) for p in sorted((ROOT / "skills" / name / "reference").glob("*.md")))
	copied.extend(f"commands/{name}.md" for name in installer.OPENCODE_COMMANDS)
	for rel in sorted(set(copied)):
		path = ROOT / rel
		check(path.is_file(), f"{rel}: the installer copies this file, but it does not exist")
		if path.is_file():
			check(installer.PROVENANCE in path.read_text(encoding="utf-8"), f"{rel}: must contain {installer.PROVENANCE!r} so the installer recognises its own copy")

	# 5b. Invocation split: a skill is either user-invoked (and hidden from the
	# model's always-loaded skill listing) or deliberately model-invocable. Claude
	# reads the SKILL.md flag; Codex reads the sibling agents/openai.yaml policy.
	# Missing either half makes an explicit-only portable skill an unintended
	# permanent per-session token cost in one of the harnesses.
	MODEL_INVOCABLE = {"review-pr", "handon"}
	for skill in sorted((ROOT / "skills").glob("*/SKILL.md")) + sorted((ROOT / "skills-claude").glob("*/SKILL.md")):
		rel = skill.relative_to(ROOT)
		fm = skill.read_text(encoding="utf-8").split("---", 2)[1]
		name_match = re.search(r"^name:\s*(\S+)", fm, re.MULTILINE)
		name = name_match.group(1) if name_match else skill.parent.name
		disabled = re.search(r"^disable-model-invocation:\s*true", fm, re.MULTILINE) is not None
		if name in MODEL_INVOCABLE:
			check(not disabled, f"{rel}: {name} is meant to be model-invocable; remove disable-model-invocation")
		else:
			check(disabled, f"{rel}: needs `disable-model-invocation: true`, or add {name!r} to MODEL_INVOCABLE in check.py")

		# Claude-only skills are never surfaced to Codex. Portable explicit-only
		# skills need the corresponding Codex policy file as well.
		if skill.parent.parent.name == "skills":
			openai_yaml = skill.parent / "agents" / "openai.yaml"
			if name in MODEL_INVOCABLE:
				if openai_yaml.exists():
					text = openai_yaml.read_text(encoding="utf-8")
					check(
						"allow_implicit_invocation: false" not in text,
						f"{openai_yaml.relative_to(ROOT)}: {name} is meant to be model-invocable",
					)
			else:
				check(openai_yaml.is_file(), f"{openai_yaml.relative_to(ROOT)}: explicit-only Codex skill policy is missing")
				if openai_yaml.is_file():
					text = openai_yaml.read_text(encoding="utf-8")
					check(
						re.search(r"(?m)^policy:\s*\n\s+allow_implicit_invocation:\s*false\s*$", text) is not None,
						f"{openai_yaml.relative_to(ROOT)}: needs policy.allow_implicit_invocation false",
					)

	# 7. Injection is idempotent, and uninstall round-trips exactly.
	block = installer.build_block(ROOT)
	check(block.startswith(f'<leos-agent version="{canonical}">'), "block header must carry the version")

	original = "# My notes\n\nSomething I wrote myself.\n"
	once = installer.inject(original, block)
	twice = installer.inject(once, block)
	check(once == twice, "inject is not idempotent: second run differs from first")
	check(original.strip() in once, "inject dropped pre-existing content")
	# Uninstall normalizes the file to a single trailing newline, which restores
	# the original exactly for any file that ended with one.
	restored = installer.strip_block(once).rstrip("\n") + "\n"
	check(restored == original, "uninstall did not restore the original content")

	stale_block = installer.inject(original, '<leos-agent version="9.9.9">\nold payload\n</leos-agent>\n')
	upgraded = installer.inject(stale_block, block)
	check("old payload" not in upgraded, "inject did not replace an older version's block")
	check(upgraded == once, "upgrading a stale block did not converge on the current content")
	check(installer.inject("", block) == block, "inject into an empty file should yield just the block")

	# Content on both sides of the block survives, and no-trailing-newline works.
	sandwich = "top\n\n" + block + "\nbottom\n"
	check("top" in installer.inject(sandwich, block) and "bottom" in installer.inject(sandwich, block), "inject lost content around the block")
	check(installer.strip_block(sandwich) == "top\n\n\nbottom\n", "strip_block mangled surrounding content")
	no_newline = "note\n\n" + block.rstrip("\n")
	check("note" in installer.strip_block(no_newline), "strip_block lost content when the block ends at EOF")

	# 8. Malformed markers must raise rather than silently swallow user content.
	dangling = "# mine\n<leos-agent>\nsecret note\n\nmore notes\n"
	check(raises_block_error(installer, dangling), "an unclosed <leos-agent> opener must refuse, not swallow content")
	check(raises_block_error(installer, "stray\n</leos-agent>\n"), "a stray closer must refuse")
	check(raises_block_error(installer, block + "\n" + block), "two blocks must refuse rather than update only the first")

	print(f"checked {checks} invariant(s)")
	if failures:
		for message in failures:
			print(f"FAIL {message}", file=sys.stderr)
		return 1
	print("all checks passed")
	return 0


if __name__ == "__main__":
	sys.exit(main())
