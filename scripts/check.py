#!/usr/bin/env python3
"""Structural checks for the leos-agent repo. Run in CI and before a release.

Asserts the version is identical across every manifest, that each harness's
manifest carries what that harness requires, and that the injection round-trips
idempotently and refuses to touch files whose markers are malformed. Stdlib only.
"""

import importlib.util
import json
import os
import re
import subprocess
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

	# 2. Claude auto-loads its default hook file. Codex supports a manifest
	# override and uses its own event list and explicit harness environment.
	claude_manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
	declared = claude_manifest.get("hooks")
	declared = declared if isinstance(declared, list) else [declared] if declared else []
	check(
		not any(str(p).endswith("hooks/hooks.json") for p in declared),
		".claude-plugin/plugin.json: must not declare hooks/hooks.json (auto-loaded; declaring it fails the plugin)",
	)
	codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
	check(codex.get("hooks") == "./hooks/hooks-codex.json", ".codex-plugin/plugin.json: must select the Codex-native hook manifest")
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
	# skills-claude/ ships through .claude-plugin/plugin.json on the same terms, so
	# it is validated on the same terms -- an unguarded tree is where conventions rot.
	for skill in skills + sorted((ROOT / "skills-claude").glob("*/SKILL.md")):
		text = skill.read_text(encoding="utf-8")
		check(text.startswith("---\n"), f"{skill.relative_to(ROOT)}: missing frontmatter")
		fm = text.split("---", 2)[1] if text.count("---") >= 2 else ""
		check(re.search(r"^name:", fm, re.MULTILINE) is not None, f"{skill.relative_to(ROOT)}: needs name")
		check(re.search(r"^description:", fm, re.MULTILINE) is not None, f"{skill.relative_to(ROOT)}: needs description")
	commands = sorted((ROOT / "commands").glob("*.md"))
	check(not commands, "commands/: duplicate native skill wrappers must not be reintroduced")

	# 6. Every path a manifest points at must exist, and hook files must parse in
	# their own harness's format. A manifest referencing a missing file ships a
	# broken plugin, so absence has to fail rather than skip.
	for rel, keys in (
		(".claude-plugin/plugin.json", ("skills", "commands", "hooks")),
		(".cursor-plugin/plugin.json", ("rules", "skills", "commands", "hooks")),
		(".codex-plugin/plugin.json", ("skills", "hooks")),
	):
		data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
		for key in keys:
			value = data.get(key)
			for declared in (value if isinstance(value, list) else [value] if value else []):
				check((ROOT / declared).exists(), f"{rel}: {key} points at {declared}, which does not exist")

	shared_hooks = ROOT / "hooks" / "hooks.json"
	check(shared_hooks.is_file(), "hooks/hooks.json is missing (Claude Code reads it)")
	if shared_hooks.is_file():
		data = json.loads(shared_hooks.read_text(encoding="utf-8"))
		check(isinstance(data.get("hooks"), dict), "hooks/hooks.json: needs a top-level `hooks` object")
	cursor_hooks = ROOT / "hooks" / "hooks-cursor.json"
	check(cursor_hooks.is_file(), "hooks/hooks-cursor.json is missing (Cursor reads it)")
	if cursor_hooks.is_file():
		data = json.loads(cursor_hooks.read_text(encoding="utf-8"))
		check(data.get("version") == 1, "hooks/hooks-cursor.json: Cursor requires version 1")
		check(isinstance(data.get("hooks"), dict), "hooks/hooks-cursor.json: needs a top-level `hooks` object")

	# 6b. The dispatch guard must actually be wired, and reachable once shipped.
	# A matcher is not decoration: without one the guard spawns a python3 per
	# Read and per Grep, which costs more than the routing it enforces. And a
	# command pointing outside package.json's `files` runs fine from a git
	# checkout and silently does nothing for anyone who installed from npm --
	# the failure that put this script in scripts/ rather than hooks/.
	shipped = tuple(entry for entry in json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["files"] if not entry.startswith("!"))
	shared_data = json.loads(shared_hooks.read_text(encoding="utf-8"))
	cursor_data = json.loads(cursor_hooks.read_text(encoding="utf-8"))
	pre = (shared_data.get("hooks") or {}).get("PreToolUse") or []
	check(bool(pre), "hooks/hooks.json: no PreToolUse entry (the dispatch guard is not wired)")
	cursor_pre = (cursor_data.get("hooks") or {}).get("subagentStart") or []
	check(bool(cursor_pre), "hooks/hooks-cursor.json: no subagentStart entry (Cursor gets no model ceiling)")
	for entry in pre:
		check(bool(entry.get("matcher")), "hooks/hooks.json: PreToolUse needs a matcher, or it spawns on every tool call")

	# Claude injects its policy at session start; Cursor reads the native rule.
	session_start = (shared_data.get("hooks") or {}).get("SessionStart") or []
	check(bool(session_start), "hooks/hooks.json: no SessionStart entry (the payload emitter is not wired)")
	for entry in session_start:
		check(bool(entry.get("matcher")), "hooks/hooks.json: SessionStart needs a matcher, or it fires on the wrong reason")
	session_start_commands = [h.get("command", "") for entry in session_start for h in entry.get("hooks") or []]
	check(
		any("scripts/emit_payload.py" in c for c in session_start_commands),
		"hooks/hooks.json: SessionStart does not resolve to scripts/emit_payload.py",
	)
	cursor_hook_keys = cursor_data.get("hooks") or {}
	check(
		all("emit_payload.py" not in entry.get("command", "") for entries in cursor_hook_keys.values() for entry in entries),
		"Cursor must observe lifecycle without injecting a duplicate policy",
	)

	# Validate every command in each native manifest, including lifecycle observers.
	commands = []
	for filename, harness_name in (("hooks.json", "claude"), ("hooks-codex.json", "codex")):
		manifest = json.loads((ROOT / "hooks" / filename).read_text())
		hooks = manifest.get("hooks", {})
		for required in ("SessionStart", "PreToolUse", "SubagentStop"):
			check(bool(hooks.get(required)), f"{filename}: missing {required}")
		for entries in hooks.values():
			for entry in entries:
				for hook in entry.get("hooks", []):
					command = hook.get("command", "")
					commands.append(command)
					check(f"LEOS_AGENT_HARNESS={harness_name}" in command, f"{filename}: hook must explicitly select {harness_name}")
					check(isinstance(hook.get("timeout"), (int, float)) and 0 < hook["timeout"] <= 10, f"{filename}: invalid timeout")
	for required in ("sessionStart", "subagentStart", "subagentStop"):
		check(bool(cursor_hook_keys.get(required)), f"Cursor: missing {required}")
	for entries in cursor_hook_keys.values():
		for hook in entries:
			commands.append(hook.get("command", ""))
			check(isinstance(hook.get("timeout"), (int, float)) and 0 < hook["timeout"] <= 10, "Cursor: invalid timeout")
	for command in commands:
		match = re.search(r"(?:\$\{[A-Z_]+\}|\./)?/?((?:scripts|hooks)/[\w./-]+\.py)", command)
		check(match is not None, f"hooks: cannot find a script path in command {command!r}")
		if match:
			target = match.group(1)
			check((ROOT / target).is_file(), f"hooks: command points at {target}, which does not exist")
			check(any(target.startswith(entry) for entry in shipped), f"hooks: {target} is outside package.json files; npm installs would not get it")

	# The guard's own modules must import cleanly: a hook that cannot even load
	# fails open on every dispatch, silently, which is the one failure mode that
	# looks exactly like everything working.
	for name in ("dispatch_guard", "dispatch_log", "usage_scan", "payload", "emit_payload"):
		path = ROOT / "scripts" / f"{name}.py"
		check(path.is_file(), f"scripts/{name}.py is missing")
		if path.is_file():
			try:
				spec = importlib.util.spec_from_file_location(f"check_{name}", path)
				module = importlib.util.module_from_spec(spec)
				spec.loader.exec_module(module)
			except Exception as exc:
				check(False, f"scripts/{name}.py does not import: {type(exc).__name__}: {exc}")

	# 6d. Determinism is the invariant the whole session-start design rests on.
	# The emitter's stdout becomes part of a cached prompt prefix; a byte that
	# varies between two runs -- a stray absolute path, a dict that iterates in a
	# different order -- turns a cache hit into a full cache write on every
	# single session, which is exactly the cost this design exists to avoid. Run
	# it twice, from two different working directories, and require the bytes to
	# match exactly.
	emit_script = ROOT / "scripts" / "emit_payload.py"
	cwds = (str(ROOT), str(ROOT.parent))
	for harness in installer.HARNESSES:
		env = dict(os.environ, LEOS_AGENT_HARNESS=harness, LEOS_AGENT_ROOT=str(ROOT))
		outputs = []
		for cwd in cwds:
			result = subprocess.run(
				[sys.executable, str(emit_script)],
				stdin=subprocess.DEVNULL,
				stdout=subprocess.PIPE,
				stderr=subprocess.PIPE,
				cwd=cwd,
				env=env,
			)
			check(
				result.returncode == 0,
				f"scripts/emit_payload.py ({harness}, cwd={cwd}): exited {result.returncode}: {result.stderr.decode('utf-8', 'replace')}",
			)
			check(bool(result.stdout.strip()), f"scripts/emit_payload.py ({harness}, cwd={cwd}): produced no output")
			outputs.append(result.stdout)
		check(
			outputs[0] == outputs[1],
			f"scripts/emit_payload.py ({harness}): output differs between two runs from different working directories",
		)
		text = outputs[0].decode("utf-8", "replace")
		for needle in ("/Users/", "/home/", str(ROOT)):
			check(needle not in text, f"scripts/emit_payload.py ({harness}): output contains an absolute path ({needle!r})")
		check(
			installer.routing.stanza(harness, installer.routing.load()) in text,
			f"scripts/emit_payload.py ({harness}): output is missing the routing stanza this machine's config renders",
		)

	# The one sentence the payload must keep: the guard refuses a dispatch that
	# names no model, and a model that does not know that wastes a turn finding
	# out. Prose elsewhere may be trimmed; this line pays for itself.
	payload_text = (ROOT / "rules" / "preferences.md").read_text(encoding="utf-8")
	check(all(tier in payload_text for tier in ("Cheap:", "Standard:", "Parent-level:")), "rules/preferences.md: missing three-tier guidance")

	# Payload files copied by the installer must carry the provenance string, or
	# it will mistake its own installed copy for a stranger's file and refuse to
	# upgrade or remove it. The list is derived from the installer's own copy sets,
	# so a skill added there can never slip past this check.
	copied = ["skills/install/SKILL.md"]
	copied.extend(f"payload/codex-agents/{name}.toml" for name in installer.CODEX_AGENTS)
	for name in installer.OPENCODE_SKILLS:
		copied.append(f"skills/{name}/SKILL.md")
		copied.extend(str(p.relative_to(ROOT)) for p in sorted((ROOT / "skills" / name / "reference").glob("*.md")))
	for rel in sorted(set(copied)):
		path = ROOT / rel
		check(path.is_file(), f"{rel}: the installer copies this file, but it does not exist")
		if path.is_file():
			check(installer.PROVENANCE in path.read_text(encoding="utf-8"), f"{rel}: must contain {installer.PROVENANCE!r} so the installer recognises its own copy")

	# An OpenCode install bakes the absolute plugin root into every copy it makes.
	# On the next upgrade the installer has to recover <plugin-root> from that
	# baked path to recognise its own work by hash -- and when it cannot, the copy
	# reads as a stranger's file, the target reports a conflict, and the whole
	# transaction aborts. That is not a hypothetical: one skill file referenced
	# only `<plugin-root>/skills/...`, the root detector looked only for
	# `/scripts/*.py`, and a single unrecognised file blocked the entire upgrade.
	# So require the round trip for every copied file, under roots chosen to be
	# awkward: one containing a plugin directory name, one containing a space.
	for root_text in ("/home/leo/.local/share/leos-agent", "/opt/agents/leos-agent",
		"/Users/leo/Library/Application Support/leos-agent"):
		for rel in sorted(set(copied)):
			if not (ROOT / rel).is_file():
				continue
			source = installer.opencode_payload(ROOT / rel, installer.PLUGIN_ROOT_TOKEN)
			baked = installer.opencode_payload(ROOT / rel, root_text)
			recovered = [baked.replace(r, installer.PLUGIN_ROOT_TOKEN) for r in installer.candidate_roots(baked)]
			check(
				baked == source or source in recovered,
				f"{rel}: candidate_roots cannot recover the plugin root baked in under {root_text!r}; "
				"an upgrade would treat this copy as a stranger's file and abort the install",
			)

	# 5a-agents. Claude Code auto-discovers agents/ at the plugin root. The set
	# must stay in lockstep with the Codex profiles (same names, one policy), and
	# each definition needs the frontmatter Claude reads — a missing model field
	# would silently inherit the parent model, which is the failure this tier
	# exists to prevent.
	claude_agents = sorted((ROOT / "agents").glob("*.md"))
	check(
		sorted(p.stem for p in claude_agents) == sorted(installer.CODEX_AGENTS),
		f"agents/: expected exactly the Claude twins of {installer.CODEX_AGENTS}, found {[p.stem for p in claude_agents]}",
	)
	for agent in claude_agents:
		rel = agent.relative_to(ROOT)
		text = agent.read_text(encoding="utf-8")
		check(text.startswith("---\n"), f"{rel}: missing frontmatter")
		fm = text.split("---", 2)[1] if text.count("---") >= 2 else ""
		name_match = re.search(r"^name:\s*(\S+)", fm, re.MULTILINE)
		check(name_match is not None and name_match.group(1) == agent.stem, f"{rel}: frontmatter name must be {agent.stem!r}")
		check(re.search(r"^description:", fm, re.MULTILINE) is not None, f"{rel}: needs description")
		check(re.search(r"^model:\s*\S", fm, re.MULTILINE) is not None, f"{rel}: needs an explicit model")
		check(re.search(r"^tools:\s*\S", fm, re.MULTILINE) is not None, f"{rel}: needs an explicit tools allowlist")

	# 5a-routing. The routing region is what makes the economical tier
	# configurable per machine. Rendering must be total (every harness gets a
	# stanza), deterministic (or a second install would not report "unchanged"),
	# and cheaper than the multi-harness prose it replaced.
	prefs_body = installer.payload_body(ROOT)
	for marker in (installer.ROUTING_OPEN, installer.ROUTING_CLOSE):
		check(prefs_body.count(marker) == 1, f"rules/preferences.md: expected exactly one {marker}")
	check(
		prefs_body.find(installer.ROUTING_OPEN) < prefs_body.find(installer.ROUTING_CLOSE),
		"rules/preferences.md: the routing region's closer precedes its opener",
	)
	for harness in installer.HARNESSES:
		rendered = installer.payload_body(ROOT, harness, {})
		check(bool(installer.routing.stanza(harness, {}).strip()), f"routing: {harness} renders an empty stanza")
		check(
			installer.ROUTING_OPEN not in rendered and installer.ROUTING_CLOSE not in rendered,
			f"routing: {harness}'s rendered payload still carries the region markers",
		)
		check(
			rendered == installer.payload_body(ROOT, harness, {}),
			f"routing: rendering {harness} twice is not byte-identical",
		)
		check(
			len(rendered.encode("utf-8")) < len(prefs_body.encode("utf-8")),
			f"routing: {harness}'s rendered payload is not smaller than the unrendered file",
		)
	# The rule is only ever installed when cursor routing is configured, so the
	# provenance requirement is checked on a configured render.
	configured_cursor = {"cursor": {"runner": {"model": "example-model", "effort": None}}}
	check(
		installer.PROVENANCE in installer.cursor_routing_rule("cursor", configured_cursor),
		f"routing: the Cursor rule must contain {installer.PROVENANCE!r} so the installer recognises its own copy",
	)

	# 5c. Plugin-root references. Skill and command text points at plugin files
	# through the <plugin-root> placeholder, and the OpenCode installer bakes the
	# absolute root into its copies — so every referenced path must actually
	# exist, or an install ships a command that can only fail.
	for base in ("skills", "skills-claude", "commands", "commands-claude"):
		for doc in sorted((ROOT / base).rglob("*.md")):
			text = doc.read_text(encoding="utf-8")
			for ref in sorted({r.rstrip(".") for r in re.findall(r"<plugin-root>/([\w./-]+)", text)}):
				check((ROOT / ref).exists(), f"{doc.relative_to(ROOT)}: <plugin-root>/{ref} does not exist")

	# The OpenCode copy of the install skill is renamed to leo-install by a
	# targeted regex in the installer; if the source name ever changes, that
	# regex would silently no-op and ship a dir/name mismatch.
	install_fm = (ROOT / "skills" / "install" / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
	check(
		re.search(r"(?m)^name:\s*install\s*$", install_fm) is not None,
		"skills/install/SKILL.md: frontmatter name must stay 'install' — leo-install.py's OpenCode rename keys on it",
	)

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
