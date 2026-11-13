#!/usr/bin/env python3
"""Structural checks for the leos-agent repo. Run in CI and before a release.

Each numbered comment in main() states one invariant and why it holds. By topic
(AGENTS.md's summary terms in brackets):

  1, 3     manifests, marketplaces and README carry one version and name
           [version agreement]
  2        each harness's manifest selects its own hook file and metadata
  4, 4a    the policy is a valid Cursor rule and keeps its load-bearing tokens
  5        skills carry name, description, and only portable keys under
           skills/ [skill frontmatter]
  5a-*     installer payloads: Codex twins, provenance, legacy hashes, OpenCode
           root recovery, agent profiles, the routing region, the install rename
  5b       every skill is user-invoked or deliberately model-invocable, and
           only a model-invocable one carries a short "Use when" trigger
           [invocation split]
  5c       bodies reach plugin files only through <plugin-root>, and every
           referenced path exists [plugin-root references]
  6, 6a    manifest paths exist and hook files parse [manifest paths]
  6b, 6c   hooks are wired, reach shipped scripts, and import cleanly
  6d       the session-start emitter is byte-identical across runs [determinism]
  7        injection is idempotent and uninstall round-trips
           [installer idempotency]
  8        malformed markers refuse rather than swallow content [marker safety]
  9        no identifying strings: denied tokens (by hash), real home paths and
           the maintainer's name in shipped prompt text [identifying strings]
  10       the Claude policy render differs from the raw one only by CLAUDE_DROP
  11       every script parses with the Python 3.9 grammar [python floor]
  12       every documented script command parses against the script's
           build_parser() [documented commands]
  13       no em dash in shipped prompt text, README, AGENTS.md, or script
           string literals [em dashes]

Stdlib only.
"""

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
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


# Frontmatter keys a portable skill under skills/ may carry: exactly what the
# skills there use today. A new key is a decision about all six harnesses, so it
# is added here on purpose rather than picked up. Claude-only extras such as
# allowed-tools and model belong under skills-claude/, which only Claude loads.
PORTABLE_SKILL_KEYS = frozenset({"name", "description", "disable-model-invocation", "argument-hint"})

# Bodies that reach a harness as text: skills and their references, commands,
# and agent profiles, whose bodies the installer copies to Codex, Cursor and
# OpenCode verbatim.
BODY_DIRS = ("agents", "skills", "skills-claude", "commands", "commands-claude")


def frontmatter_keys(text):
	"""Top-level keys of a Markdown file's YAML frontmatter, in order."""
	if not text.startswith("---\n") or text.count("---") < 2:
		return []
	return re.findall(r"(?m)^([A-Za-z0-9_-]+)[ \t]*:", text.split("---", 2)[1])


def non_portable_keys(text):
	return [key for key in frontmatter_keys(text) if key not in PORTABLE_SKILL_KEYS]


def slash_skill_names(frontmatter, skills):
	"""Skill names a frontmatter description spells as `/name` commands."""
	match = re.search(r"(?m)^description:(.*)$", frontmatter)
	names = {skill.parent.name for skill in skills}
	return sorted(name for name in names if match and re.search(r"(?<![\w/])/" + re.escape(name) + r"\b", match.group(1)))


# A model-invocable skill's description sits in every session's skill listing
# and is the only thing the model reads when deciding to load it.
TRIGGER_RE = re.compile(r"\b[Uu]se (?:it |this )?when\b")
MODEL_INVOCABLE_DESCRIPTION_CAP = 300


def frontmatter_description(text):
	"""The description value, with a folded or literal block joined to one line."""
	if not text.startswith("---\n") or text.count("---") < 2:
		return ""
	lines = text.split("---", 2)[1].splitlines()
	for index, line in enumerate(lines):
		match = re.match(r"description:[ \t]*(.*?)\s*$", line)
		if not match:
			continue
		value = match.group(1)
		if value[:1] not in (">", "|"):
			return value.strip("\"'")
		block = []
		for following in lines[index + 1:]:
			if following and not following[0].isspace():
				break
			block.append(following.strip())
		return " ".join(part for part in block if part)
	return ""


def skill_trigger_problems(description, model_invocable):
	"""A model-invocable skill says when to load it, briefly; a user-invoked one
	does not, since the model never chooses it and a trigger would invite that."""
	problems = []
	has_trigger = TRIGGER_RE.search(description) is not None
	if model_invocable:
		if not has_trigger:
			problems.append("model-invocable description needs a 'Use when ...' trigger clause")
		size = len(description.encode("utf-8"))
		if size > MODEL_INVOCABLE_DESCRIPTION_CAP:
			problems.append(f"model-invocable description is {size} bytes, over the {MODEL_INVOCABLE_DESCRIPTION_CAP}-byte cap")
	elif has_trigger:
		problems.append("user-invoked description must not carry a 'Use when ...' trigger clause")
	return problems


def plugin_root_violations(text):
	"""Ways a skill, command, or agent body reaches a plugin file non-portably.

	Claude Code substitutes ${CLAUDE_PLUGIN_ROOT} when it loads such a body, but
	Codex, Cursor, OpenCode, Hermes and Pi read it verbatim, so a path built on
	it runs `python3 /scripts/...` there. Even Claude Code leaves the variable
	out of the environment of commands the model runs, so the unbraced form is
	not expanded anywhere. Every script path therefore goes through <plugin-root>,
	which each skill resolves first and the OpenCode installer bakes into its
	copies. Naming CLAUDE_PLUGIN_ROOT as a place to look is fine; building a path
	from it, or from anything but the placeholder, is the bug.
	"""
	problems = []
	if re.search(r"CLAUDE_PLUGIN_ROOT\}?/", text):
		problems.append("builds a path from CLAUDE_PLUGIN_ROOT; write <plugin-root>/... and resolve it first")
	for prefix in re.findall(r'(?m)^\s*python3\s+"?(\S*?)scripts/[\w.-]+\.py', text):
		if prefix != "<plugin-root>/":
			problems.append(f"runs {prefix or './'}scripts/...; every script invocation goes through <plugin-root>/scripts/")
	return problems


# 9. Identifying strings. Digests of "leos-agent-deny-v1:" + a lowercased token
# (an organization, a repo, a ticket key, a PR number, a home directory name, an
# SSH alias) that must not appear in any text file of the repo. Only hashes are
# kept here so the gate does not itself carry what it forbids. Add an entry with
# `python3 scripts/check.py --deny-hash WORD`.
DENY_PREFIX = "leos-agent-deny-v1:"
DENY_SHA256 = frozenset({
	"0dcdf1d7af920e171ba3dc3b0418934759bfb6cb518e0f51e4cfb6e5dec46eb3",
	"68e576077dc2715ede8756b3d7963e766f1329b37b69c9f784ebe15f7d22b02c",
	"f413302b04ff57c72a2e699ea939895312449e674ac39fa8264035277c9f5c65",
	"4912806a9eb72882c617c294865d825b59c1aa7ec0aba32fcaef2ebbba7b0852",
	"a1c6748d6fe92eb7c4bf78308cf18045801ba5852987418a84732d59ea474ea0",
	"e81615314d70b022c16710f36ee6caac5b76d1a608763747759a807e3bac9848",
	"0d27418c2b083dcaef97dfaa19fee1525658864ace0cb8619e05b35291336d52",
	"f5d25be253619988bca47ba55d45d61e1d8a8210db8bd7840723c225f4c7613d",
	"c1a0228e55e823d94ea93e1d1a407a6d7b325e621d9de2d6042785a667903e4d",
	"fcb6effa98ec7dc294b90c9e69b84026c6fc1f34de8178a49ca53e422cdfc649",
})
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# Shipped prompt text: what reaches a model or a user's machine as instructions.
PROMPT_DIRS = ("skills", "skills-claude", "agents", "rules", "payload/codex-agents")
PROMPT_DOCS = ("README.md", "AGENTS.md", "hooks/README.md")
HOME_PATH_RE = re.compile(r"/Users/(?!<|you\b|me\b)[A-Za-z0-9._-]+/")
OWNER_NAME_RE = re.compile(r"\bLeo(?:'s)?\b")


def deny_digest(word):
	return hashlib.sha256((DENY_PREFIX + word.lower()).encode("utf-8")).hexdigest()


def token_words(token):
	"""The token and every hyphen-joined run of its parts, so a denied word
	inside a longer slug (fix/abc-123-foo) still matches."""
	parts = [p for p in re.split(r"[-_]", token) if p]
	words = {token}
	for i in range(len(parts)):
		for j in range(i + 1, len(parts) + 1):
			words.add("-".join(parts[i:j]))
	return words


def repo_text_files(root):
	"""Relative paths of the repo's files: tracked plus untracked-not-ignored in a
	git checkout, otherwise everything outside .git and node_modules."""
	root = Path(root)
	paths = None
	if (root / ".git").exists():
		env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
		run = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
			cwd=root, env=env, capture_output=True)
		if run.returncode == 0:
			paths = sorted({p for p in run.stdout.decode("utf-8", "replace").split("\0") if p})
	if paths is None:
		paths = sorted(
			str(f.relative_to(root)) for f in root.rglob("*")
			if f.is_file() and not {".git", "node_modules"} & set(f.relative_to(root).parts)
		)
	skipped = []
	for rel in paths:
		parts = Path(rel).parts
		if "results" in parts and parts[0] == "evals":
			continue
		if (root / rel).is_file():
			skipped.append(rel)
	return skipped


def identifying_string_problems(root, deny=None):
	"""File:line problems for denied tokens, real home paths, and the maintainer's
	name in prompt text. Messages never repeat the offending text."""
	root = Path(root)
	deny = DENY_SHA256 if deny is None else deny
	problems = []
	for rel in repo_text_files(root):
		try:
			data = (root / rel).read_bytes()
			if b"\0" in data:
				continue
			text = data.decode("utf-8")
		except (OSError, UnicodeDecodeError):
			continue
		posix = Path(rel).as_posix()
		is_prompt = any(posix == d or posix.startswith(d + "/") for d in PROMPT_DIRS)
		check_paths = is_prompt or posix in PROMPT_DOCS
		for number, line in enumerate(text.splitlines(), 1):
			hashed = {deny_digest(word) for tok in TOKEN_RE.findall(line) for word in token_words(tok)}
			if hashed & deny:
				problems.append(f"{posix}:{number}: contains a denied identifying token (see DENY_SHA256 in scripts/check.py)")
			if check_paths and HOME_PATH_RE.search(line):
				problems.append(f"{posix}:{number}: names a real home directory; write /Users/<you>/")
			if is_prompt and OWNER_NAME_RE.search(line):
				problems.append(f"{posix}:{number}: names the maintainer in prompt text; write 'the user'")
	return problems


# 11. Python floor. CI runs 3.9 and 3.14, but a newer-only construct in a
# script that CI never imports would still pass there and fail on stock macOS
# Python 3.9. Parse every script with the 3.9 grammar.
PYTHON_FLOOR = (3, 9)


def python_floor_problems(root):
	problems = []
	for path in sorted((Path(root) / "scripts").glob("*.py")):
		try:
			ast.parse(path.read_text(encoding="utf-8"), filename=path.name, feature_version=PYTHON_FLOOR)
		except SyntaxError as exc:
			problems.append(f"scripts/{path.name}:{exc.lineno}: not Python {PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]} syntax: {exc.msg}")
	return problems


# 12. Documented commands. A skill line is what the model runs, so a flag the
# script does not take fails at the worst moment. Each documented line is
# parsed against the script's own build_parser().
COMMAND_DIRS = ("skills", "skills-claude")
COMMAND_RE = re.compile(r'python3 "<plugin-root>/scripts/([\w-]+)\.py"')
FENCE_RE = re.compile(r"\s*(`{3,}|~{3,})")
HEX40 = "a" * 40
# Placeholders the docs use, and a value of the right shape for each.
PLACEHOLDERS = {
	"N": "1", "P": "1", "ID": "1", "SHA": HEX40, "FULL_SHA": HEX40, "PREV": HEX40, "HEAD": HEX40,
	"TOKEN": "c" * 32, "OWNER/REPO": "o/r", "FILE": "/tmp/x", "FP": "none", "EVENT": "COMMENT", "CMD": "x",
}
# Angle-bracket placeholders whose value is checked against choices.
ANGLE_PLACEHOLDERS = {"<harness>": "claude"}
# (file, script) pairs whose documented line is deliberately not a whole
# command. Empty: every documented line parses. An entry needs a reason.
PARTIAL_COMMANDS = frozenset()


def documented_commands(text):
	"""(line number, script, argument text) for each documented script command.

	In a fenced block or on a bare line the command runs to the end of the line,
	with backslash continuations joined. In inline code it runs to the closing
	backtick, which may sit on a later line of the same paragraph."""
	lines = text.splitlines()
	found = []
	fence = None
	for index, line in enumerate(lines):
		marker = FENCE_RE.match(line)
		if marker:
			char = marker.group(1)[0]
			if fence is None:
				fence = char
			elif char == fence:
				fence = None
			continue
		for match in COMMAND_RE.finditer(line):
			rest = line[match.end():]
			if fence is None and line[:match.start()].count("`") % 2 == 1:
				following = index + 1
				while "`" not in rest and following < len(lines) and lines[following].strip():
					rest += " " + lines[following].strip()
					following += 1
				rest = rest.split("`", 1)[0]
			else:
				following = index + 1
				while rest.rstrip().endswith("\\") and following < len(lines):
					rest = rest.rstrip()[:-1] + " " + lines[following].strip()
					following += 1
			found.append((index + 1, match.group(1), rest))
	return found


def command_argv(rest):
	"""The argv a documented argument text stands for: optional [groups] dropped,
	placeholders filled, the shell comment, pipe or redirect cut off."""
	previous = None
	while previous != rest:
		previous, rest = rest, re.sub(r"\[[^\[\]]*\]", " ", rest)
	for placeholder, value in ANGLE_PLACEHOLDERS.items():
		rest = rest.replace(placeholder, value)
	rest = re.sub(r"<[^<>\n]*>", "x", rest)
	rest = rest.replace("$SCRATCH", "/tmp/x")
	rest = re.sub(r"PRRT_(?:\u2026|\.\.\.)", "PRRT_x", rest)
	lexer = shlex.shlex(rest, posix=True, punctuation_chars=True)
	lexer.whitespace_split = True
	argv = []
	for token in lexer:
		if set(token) <= set("();<>|&"):
			break
		if token in ("...", "\u2026"):
			continue
		argv.append(PLACEHOLDERS.get(token, token))
	return argv


def load_script(path):
	spec = importlib.util.spec_from_file_location("check_cli_" + path.stem.replace("-", "_"), path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def documented_command_problems(root):
	"""file:line problems for documented script commands that do not parse."""
	root = Path(root)
	scripts = str(root / "scripts")
	added = scripts not in sys.path
	if added:
		sys.path.insert(0, scripts)  # scripts import their siblings
	try:
		return _documented_command_problems(root)
	finally:
		if added:
			sys.path.remove(scripts)


def _documented_command_problems(root):
	parsers = {}
	problems = []
	for base in COMMAND_DIRS:
		for doc in sorted((root / base).rglob("*.md")):
			rel = doc.relative_to(root).as_posix()
			for number, script, rest in documented_commands(doc.read_text(encoding="utf-8")):
				path = root / "scripts" / f"{script}.py"
				if (rel, script) in PARTIAL_COMMANDS or not path.is_file():
					continue  # a missing script is 5c's failure
				if script not in parsers:
					try:
						parsers[script] = load_script(path).build_parser
					except AttributeError:
						parsers[script] = None
				if parsers[script] is None:
					problems.append(f"{rel}:{number}: scripts/{script}.py has no build_parser(); add one")
					continue
				argv = command_argv(rest)
				err = io.StringIO()
				try:
					with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
						parsers[script]().parse_args(argv)
				except SystemExit as exc:
					if exc.code:
						said = [l for l in err.getvalue().splitlines() if "error:" in l]
						reason = said[-1].split("error:", 1)[1].strip() if said else f"exit {exc.code}"
						problems.append(f"{rel}:{number}: {script}.py {' '.join(argv)}: {reason}")
	return problems


# 13. Em dashes. Prompt text, docs and what scripts print read as a person
# wrote them, and these two dashes are the tell. A script constant that names
# the dash only to refuse or parse it is listed here by its variable name.
DASHES = ("\u2014", "\u2015")
DASH_DETECTORS = {
	"scripts/check.py": {"DASHES"},
	"scripts/ghreview.py": {"EM_DASHES"},
	"scripts/outcome.py": {"_SEP"},
}


def em_dash_problems(root):
	root = Path(root)
	problems = []
	docs = [root / rel for rel in PROMPT_DOCS]
	for base in PROMPT_DIRS:
		docs.extend(sorted(p for p in (root / base).rglob("*") if p.is_file()))
	for doc in docs:
		try:
			text = doc.read_text(encoding="utf-8")
		except (OSError, UnicodeDecodeError):
			continue
		for number, line in enumerate(text.splitlines(), 1):
			if any(dash in line for dash in DASHES):
				problems.append(f"{doc.relative_to(root).as_posix()}:{number}: em dash; rewrite the sentence")
	for path in sorted((root / "scripts").glob("*.py")):
		rel = f"scripts/{path.name}"
		try:
			tree = ast.parse(path.read_text(encoding="utf-8"))
		except SyntaxError:
			continue  # 11 reports it
		skip = set()
		for node in ast.walk(tree):
			if isinstance(node, ast.Assign) and any(
				isinstance(t, ast.Name) and t.id in DASH_DETECTORS.get(rel, ()) for t in node.targets):
				skip.update(id(n) for n in ast.walk(node.value))
		for node in ast.walk(tree):
			if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
				and any(dash in node.value for dash in DASHES)):
				problems.append(f"{rel}:{node.lineno}: string literal holds an em dash; rewrite it")
	return sorted(set(problems), key=lambda p: (p.split(":")[0], int(p.split(":")[1])))


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

	# 3. Marketplaces parse, name this plugin, and carry no stale version. Claude's
	# entry must carry one; the Codex marketplace carries none today, and one added
	# there would have to match too (and be taught to bump.py).
	for rel in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
		data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
		plugins = data.get("plugins", [])
		check(len(plugins) == 1, f"{rel}: expected exactly one plugin entry")
		if plugins:
			entry = plugins[0]
			check(entry.get("name") == NAME, f"{rel}: plugin name != {NAME!r}")
			if rel.startswith(".claude-plugin") or "version" in entry:
				check(entry.get("version") == canonical, f"{rel}: plugin version {entry.get('version')!r} != {canonical!r}")

	# Every version string the README hardcodes must be the current one: the
	# uninstall commands point at versioned cache paths.
	readme = (ROOT / "README.md").read_text(encoding="utf-8")
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
		# Every harness loads skills/; only Claude loads skills-claude/. A key one
		# harness gives meaning to and another ignores is a silent difference.
		if skill.parent.parent.name == "skills":
			extra = non_portable_keys(text)
			check(not extra, f"{skill.relative_to(ROOT)}: non-portable frontmatter {extra}; Claude-only keys "
				"belong under skills-claude/, and a new portable key goes in PORTABLE_SKILL_KEYS on purpose")
			# A description is discovery text on all six harnesses, and only some
			# spell skill invocation `/name`. Name the skill, not the Claude command.
			slashed = slash_skill_names(fm, skills)
			check(not slashed, f"{skill.relative_to(ROOT)}: description names {slashed} as slash commands; "
				"say 'the <name> skill' so it reads on every harness")
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

	# 6a. Hook files parse in their own harness's format.
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

	# 6c. Claude injects its policy at session start; Cursor reads the native
	# rule. Below, every hook command in every native manifest, observers
	# included, names its harness, keeps a sane timeout, and reaches a shipped
	# script, and the modules those scripts load import cleanly.
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
	for name in ("dispatch_guard", "dispatch_log", "outcome", "observe_agent", "handback_contract", "accounting", "usage_scan", "payload", "emit_payload", "compact_nudge"):
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

	# 4a. The one sentence the payload must keep: the guard refuses a dispatch
	# that names no model, and a model that does not know that wastes a turn
	# finding out. Prose elsewhere may be trimmed; this line pays for itself.
	payload_text = (ROOT / "rules" / "preferences.md").read_text(encoding="utf-8")
	check(all(tier in payload_text for tier in ("Cheap:", "Standard:", "Parent-level:")), "rules/preferences.md: missing three-tier guidance")
	# The escalation contract is two tokens the guard and the observer parse:
	# the brief header and the worker's closing line. Prose around them may
	# change; the tokens themselves are load-bearing on every harness.
	check("Escalation from" in payload_text and "Result:" in payload_text,
		"rules/preferences.md: missing the escalation contract (Escalation from / Result:)")

	# 5a-twins. The Codex TOMLs are hand-maintained twins of agents/*.md. Nothing
	# syncs them, so a contract edited into one body and not the other would ship
	# two different workers under one tier name. Compare the decoded string
	# exactly.
	for name in installer.CODEX_AGENTS:
		md = ROOT / "agents" / f"{name}.md"
		toml = ROOT / "payload" / "codex-agents" / f"{name}.toml"
		if not (md.is_file() and toml.is_file()):
			continue
		body = md.read_text(encoding="utf-8").split("---", 2)[2].lstrip("\n")
		match = re.search(r'(?m)^developer_instructions = "(.*)"$', toml.read_text(encoding="utf-8"))
		try:
			decoded = json.loads('"' + match.group(1) + '"') if match else None
		except ValueError:
			decoded = None
		check(decoded == body, f"payload/codex-agents/{name}.toml: developer_instructions differs from agents/{name}.md body")

	# 5a-copies. Payload files copied by the installer must carry the provenance
	# string, or it will mistake its own installed copy for a stranger's file and
	# refuse to upgrade or remove it. The list is derived from the installer's
	# own copy sets, so a skill added there can never slip past this check.
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

	# 5a-legacy. Frozen pre-v12 ownership evidence, not an inventory of current
	# payloads.
	legacy = json.loads((ROOT / "payload/legacy-copy-hashes.json").read_text())
	check(legacy.get("schema") == 1, "legacy hashes: unsupported schema")
	check(bool(re.fullmatch(r"[0-9a-f]{40}", str(legacy.get("source_commit", "")))),
		"legacy hashes: source must be a full commit SHA")
	hashes = legacy.get("sha256")
	check(isinstance(hashes, list) and bool(hashes)
		and all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)
		and hashes == sorted(set(hashes)), "legacy hashes: expected sorted unique SHA-256 digests")

	# 5a-roots. An OpenCode install bakes the absolute plugin root into every
	# copy it makes. On the next upgrade the installer has to recover
	# <plugin-root> from that baked path to recognise its own work by hash -- and
	# when it cannot, the copy reads as a stranger's file, the target reports a
	# conflict, and the whole transaction aborts. That is not a hypothetical: one
	# skill file referenced only `<plugin-root>/skills/...`, the root detector
	# looked only for `/scripts/*.py`, and a single unrecognised file blocked the
	# entire upgrade. So require the round trip for every copied file, under
	# roots chosen to be awkward: one containing a plugin directory name, one
	# containing a space.
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
	# each definition needs the frontmatter Claude reads. A missing model field
	# would silently inherit the parent model, which is the failure this tier
	# exists to prevent.
	claude_agents = sorted((ROOT / "agents").glob("*.md"))
	# Retired profiles are no longer installed; their sources may linger until deleted.
	shipped_agents = [p.stem for p in claude_agents if p.stem not in installer.RETIRED_AGENTS]
	check(
		sorted(shipped_agents) == sorted(installer.CODEX_AGENTS),
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
		# On Cursor this sentence is the only form the no-delegation rule takes:
		# native_agent copies the body verbatim and Cursor publishes no per-agent
		# tool restriction. Editing it away during prose tidying would silently
		# remove the rule on that harness, so it is load-bearing, not decorative.
		if agent.stem != "leo-reviewer":
			check("do not spawn further agents" in text.lower(),
				f"{rel}: the worker body must keep its no-delegation sentence; on Cursor it is the only carrier")

	# Unconfigured routing must omit the model key. Writing Claude Code's
	# "inherit" into a Cursor or OpenCode agent states a model identifier the
	# harness cannot resolve, and claims a routing decision nobody made.
	for harness in ("cursor", "opencode"):
		for name in installer.CODEX_AGENTS:
			fm = installer.native_agent(ROOT, name, harness, {}).split("---", 2)[1]
			check("model:" not in fm, f"{harness}/{name}: unconfigured routing must omit model:, not invent one")
			configured = {harness: {"cheap": {"model": "example-model", "effort": None},
				"standard": {"model": "example-model", "effort": None}}}
			rendered = installer.native_agent(ROOT, name, harness, configured)
			check(installer.owned_copy(rendered), f"{harness}/{name}: the installer must recognise its own copy")

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

	# 5c. Plugin-root references. Skill, command, and agent text points at plugin
	# files through the <plugin-root> placeholder and nothing else (see
	# plugin_root_violations for why ${CLAUDE_PLUGIN_ROOT} is not portable), and
	# the OpenCode installer bakes the absolute root into its copies, so every
	# referenced path must actually exist, or an install ships a command that can
	# only fail.
	for base in BODY_DIRS:
		for doc in sorted((ROOT / base).rglob("*.md")):
			text = doc.read_text(encoding="utf-8")
			problems = plugin_root_violations(text)
			check(not problems, f"{doc.relative_to(ROOT)}: " + "; ".join(problems))
			for ref in sorted({r.rstrip(".") for r in re.findall(r"<plugin-root>/([\w./-]+)", text)}):
				check((ROOT / ref).exists(), f"{doc.relative_to(ROOT)}: <plugin-root>/{ref} does not exist")

	# 5a-rename. The OpenCode copy of the install skill is renamed to leo-install
	# by a targeted regex in the installer; if the source name ever changes, that
	# regex would silently no-op and ship a dir/name mismatch.
	install_fm = (ROOT / "skills" / "install" / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
	check(
		re.search(r"(?m)^name:\s*install\s*$", install_fm) is not None,
		"skills/install/SKILL.md: frontmatter name must stay 'install'; leo-install.py's OpenCode rename keys on it",
	)

	# 5b. Invocation split: a skill is either user-invoked (and hidden from the
	# model's always-loaded skill listing) or deliberately model-invocable. Claude
	# reads the SKILL.md flag; Codex reads the sibling agents/openai.yaml policy.
	# Missing either half makes an explicit-only portable skill an unintended
	# permanent per-session token cost in one of the harnesses.
	MODEL_INVOCABLE = {"review-pr", "handon"}
	for skill in sorted((ROOT / "skills").glob("*/SKILL.md")) + sorted((ROOT / "skills-claude").glob("*/SKILL.md")):
		rel = skill.relative_to(ROOT)
		skill_text = skill.read_text(encoding="utf-8")
		fm = skill_text.split("---", 2)[1]
		name_match = re.search(r"^name:\s*(\S+)", fm, re.MULTILINE)
		name = name_match.group(1) if name_match else skill.parent.name
		disabled = re.search(r"^disable-model-invocation:\s*true", fm, re.MULTILINE) is not None
		if name in MODEL_INVOCABLE:
			check(not disabled, f"{rel}: {name} is meant to be model-invocable; remove disable-model-invocation")
		else:
			check(disabled, f"{rel}: needs `disable-model-invocation: true`, or add {name!r} to MODEL_INVOCABLE in check.py")
		# 5b-trigger. The description is the model's only cue to load a
		# model-invocable skill, and costs bytes in every session's listing.
		for problem in skill_trigger_problems(frontmatter_description(skill_text), not disabled):
			check(False, f"{rel}: {problem}")

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

	# 7. Installer idempotency, through the shipped CLI. A sandboxed install, a
	# rerun that must leave every byte alone, and an uninstall that must give the
	# config directory back exactly as it was, user comments included. Checking
	# the block helpers alone passed while the installer itself drifted.
	import tempfile
	block = installer.build_block(ROOT)
	check(block.startswith(f'<leos-agent version="{canonical}">'), "block header must carry the version")

	def snapshot(base):
		return {p.relative_to(base).as_posix(): (p.read_bytes() if p.is_file() else None)
			for p in sorted(base.rglob("*")) if not p.is_symlink()}

	with tempfile.TemporaryDirectory(prefix="leo check ") as tmp:
		sandbox = Path(tmp)
		cfgs = {name: sandbox / name for name in ("codex", "opencode", "claude")}
		for path in (*cfgs.values(), sandbox / "home"):
			path.mkdir()
		user_config = '{\n  // keep this comment\n  "theme": "dark",\n}\n'
		(cfgs["opencode"] / "opencode.jsonc").write_text(user_config)
		(cfgs["claude"] / "CLAUDE.md").write_text('# Mine\n\n<leos-agent version="9.9.9">\nold payload\n</leos-agent>\n')
		env = {k: v for k, v in os.environ.items() if k not in ("OPENCODE_CONFIG", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT")}
		env.update(HOME=str(sandbox / "home"), CODEX_HOME=str(cfgs["codex"]), OPENCODE_CONFIG_DIR=str(cfgs["opencode"]),
			CLAUDE_CONFIG_DIR=str(cfgs["claude"]), HERMES_HOME=str(sandbox / "hermes"),
			PI_CODING_AGENT_DIR=str(sandbox / "pi"), XDG_CONFIG_HOME=str(sandbox / "xdg"),
			LEOS_AGENT_LOCAL_PATH=str(sandbox / "data"), LEOS_AGENT_ROOT=str(ROOT),
			LEOS_AGENT_PRICE_REFRESH="off", PYTHONDONTWRITEBYTECODE="1")

		def install(harness, *flags):
			result = subprocess.run([sys.executable, str(ROOT / "scripts" / "leo-install.py"), harness, *flags],
				env=env, cwd=str(sandbox), capture_output=True, text=True, timeout=60)
			check(result.returncode == 0, f"leo-install.py {harness} {' '.join(flags)} exited {result.returncode}: "
				f"{(result.stdout + result.stderr).strip()[-400:]}")
			return result

		for harness in ("codex", "opencode"):
			before = snapshot(cfgs[harness])
			install(harness)
			installed = snapshot(cfgs[harness])
			check(installed != before, f"installer idempotency ({harness}): the install wrote nothing")
			rerun = install(harness)
			check(snapshot(cfgs[harness]) == installed, f"installer idempotency ({harness}): a rerun changed bytes")
			check("\n0 target(s) changed" in rerun.stdout, f"installer idempotency ({harness}): a rerun reported changes")
			install(harness, "--check")
			install(harness, "--uninstall")
			check(snapshot(cfgs[harness]) == before,
				f"installer idempotency ({harness}): uninstall did not restore the config directory byte for byte")
		check((cfgs["opencode"] / "opencode.jsonc").read_text() == user_config, "uninstall changed the user's JSONC")

		install("claude")
		migrated = (cfgs["claude"] / "CLAUDE.md").read_text()
		check(migrated == "# Mine\n", "migration did not take back exactly the legacy block")
		install("claude")
		check((cfgs["claude"] / "CLAUDE.md").read_text() == migrated, "a second migration changed the file")

	# 8. Malformed markers must raise rather than silently swallow user content.
	dangling = "# mine\n<leos-agent>\nsecret note\n\nmore notes\n"
	check(raises_block_error(installer, dangling), "an unclosed <leos-agent> opener must refuse, not swallow content")
	check(raises_block_error(installer, "stray\n</leos-agent>\n"), "a stray closer must refuse")
	check(raises_block_error(installer, block + "\n" + block), "two blocks must refuse rather than update only the first")

	# 9. No identifying strings anywhere in the repo's text, and no real home
	# paths or maintainer name in shipped prompt text.
	for problem in identifying_string_problems(ROOT):
		check(False, f"identifying strings: {problem}")
	check(True, "identifying strings")

	# 10. The Claude-only policy drops must not orphan. Every CLAUDE_DROP entry has
	# to match rules/preferences.md (an edit to the policy would otherwise stop
	# dropping silently, or make the render fail at session start), and the Claude
	# render may differ from the raw render only by those drops: strip them from
	# the raw text by hand and the two must be equal.
	sys.path.insert(0, str(ROOT / "scripts"))
	import payload as payload_module
	raw_policy = payload_module.payload_body(ROOT, "codex", {})
	check(bool(payload_module.CLAUDE_DROP), "CLAUDE_DROP is empty; delete the mechanism or add entries")
	for entry in payload_module.CLAUDE_DROP:
		hits = payload_module._drop_pattern(entry).findall(raw_policy)
		check(len(hits) == 1, f"CLAUDE_DROP entry matches {len(hits)} time(s) in rules/preferences.md, want 1: {entry!r}")
	if all(len(payload_module._drop_pattern(e).findall(raw_policy)) == 1 for e in payload_module.CLAUDE_DROP):
		by_hand = raw_policy
		for entry in payload_module.CLAUDE_DROP:
			by_hand = payload_module._drop_pattern(entry).sub("", by_hand, count=1)
		claude_policy = payload_module.payload_body(ROOT, "claude", {})
		routing_claude = payload_module.routing.stanza("claude", {})
		routing_codex = payload_module.routing.stanza("codex", {})
		check(by_hand.replace(routing_codex, routing_claude) == claude_policy,
			"the Claude render differs from the raw policy by more than CLAUDE_DROP")

	# 11. Every script parses with the Python 3.9 grammar.
	for problem in python_floor_problems(ROOT):
		check(False, f"python floor: {problem}")
	check(True, "python floor")

	# 12. Every documented `python3 "<plugin-root>/scripts/X.py" ...` line parses
	# against X.build_parser().
	for problem in documented_command_problems(ROOT):
		check(False, f"documented command: {problem}")
	check(True, "documented commands")

	# 13. No em dash in shipped prompt text, the docs, or script strings.
	for problem in em_dash_problems(ROOT):
		check(False, f"em dashes: {problem}")
	check(True, "em dashes")

	print(f"checked {checks} invariant(s)")
	if failures:
		for message in failures:
			print(f"FAIL {message}", file=sys.stderr)
		return 1
	print("all checks passed")
	return 0


if __name__ == "__main__":
	if len(sys.argv) == 3 and sys.argv[1] == "--deny-hash":
		print(deny_digest(sys.argv[2]))
		sys.exit(0)
	sys.exit(main())
