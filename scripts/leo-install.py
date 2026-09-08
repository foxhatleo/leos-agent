#!/usr/bin/env python3
"""Install what a harness's plugin system cannot deliver on its own.

The payload itself lives in rules/preferences.md and is read live by each
harness's plugin (a SessionStart hook, or Cursor's alwaysApply rule) at
session start, so this tool no longer writes it into any global instruction
file. What is left to install is per-harness and small: Codex's agent
profiles (its plugins cannot ship custom agent definitions), Cursor's
per-machine routing rule, and OpenCode's skill and command copies (its
plugins cannot register those from JS either). It also cleans up the
<leos-agent> block an earlier version of this tool left in a harness's
former global file, now that nothing writes there.

Acts on exactly ONE harness per run -- the one named on the command line. A
session running in Codex installs Codex and nothing else.

Usage:
    leo-install.py <harness> [--dry-run | --uninstall | --check] [--force]

Harnesses: claude, codex, cursor, hermes, pi, opencode
"""

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routing  # noqa: E402  owns the harness list and the machine-local model config

HARNESSES = routing.HARNESSES

# Rendering lives in payload.py so that the installer and the session-start
# emitter share one implementation. Re-exported here because check.py,
# measure_context.py and the tests all reach for these through this module.
from payload import (  # noqa: E402,F401
	CLOSE_RE,
	OPEN_RE,
	ROUTING_CLOSE,
	ROUTING_OPEN,
	build_block,
	payload_body,
	plugin_root,
	read_version,
	render_routing,
)

# Copied payload files carry this string, which is how uninstall tells its own
# copies apart from a file the user happens to have put at the same path.
PROVENANCE = "leos-agent"

# Skill and command text refers to scripts as <plugin-root>/scripts/… and the
# model resolves the root once. OpenCode copies live apart from the scripts,
# and OpenCode sets no resolution env var, so their copies are installed with
# the token already replaced by this machine's absolute plugin root. Installing
# alternately from a checkout and a cache re-bakes the root each time — last
# install wins, and --check reports a stale root as out of date.
PLUGIN_ROOT_TOKEN = "<plugin-root>"

# OpenCode plugins cannot register skills or commands from JS, so these are
# copied to disk instead. check.py asserts every file they name carries
# PROVENANCE, without which the installer would refuse to upgrade its own copy.
OPENCODE_SKILLS = ("doctor", "review-pr", "handoff", "handon", "tune-routing", "review-usage")
OPENCODE_COMMANDS = ("review-pr", "handoff", "handon")

# Codex plugins cannot package custom agent definitions directly, so these are
# copied into ~/.codex/agents. Keep this tuple authoritative: check.py and the
# installer tests derive the expected payload from it.
CODEX_AGENTS = ("leo-cheap", "leo-standard", "leo-parent", "leo-reviewer", "leo-runner", "leo-executor")


class BlockError(Exception):
	"""The target file's markers are malformed; editing it could destroy content."""


class Result:
	"""What happened to one target, for the run report."""

	def __init__(self, target, status, detail="", diff=""):
		self.target = target
		self.status = status
		self.detail = detail
		self.diff = diff

	@property
	def changed(self):
		return self.status in ("created", "updated", "removed", "migrated")

	@property
	def failed(self):
		return self.status in ("error", "conflict")

	def line(self, pending):
		status = self.status
		if pending and self.changed:
			status = {
				"created": "to create",
				"updated": "to update",
				"removed": "to remove",
				"migrated": "to migrate",
			}[status]
		suffix = f" ({self.detail})" if self.detail else ""
		return f"  {status:9} {self.target}{suffix}"


def render_codex_agent(text, agent_name, config):
	"""Profiles carry instructions; the dispatch guard enforces model selection."""
	return re.sub(r'(?m)^model(?:_reasoning_effort)? = .*\n', "", text)


def cursor_routing_rule(harness, config):
	"""Cursor reads its rules straight out of the plugin, so the per-machine half
	has to arrive as its own always-applied rule file."""
	return (
		"---\n"
		"description: leos-agent model routing for this machine.\n"
		"alwaysApply: true\n"
		"---\n"
		"This supersedes the model-routing dispatch line in Leo's agent operating\n"
		"preferences:\n\n"
		f"{routing.stanza(harness, config)}\n"
	)


def opencode_routing_rule(harness, config):
	"""OpenCode reads rules/preferences.md straight off disk through `instructions`,
	which means it reads it UN-rendered -- the routing region keeps its shipped
	default and this machine's config never reaches the model. Cursor has the
	same shape and the same answer: ship the per-machine half as its own file.
	"""
	return (
		"<!-- leos-agent -->\n"
		"This supersedes the model-routing dispatch line in Leo's agent operating\n"
		"preferences:\n\n"
		f"{routing.stanza(harness, config)}\n"
	)


def scan_markers(text):
	"""Find marker lines, ignoring any inside a fenced code block.

	These files are Markdown, and a fenced example showing the block format is a
	perfectly reasonable thing for someone to keep in their own notes. Treating
	such an example as a real marker would either overwrite it or wedge the file
	into a permanent "two blocks" error, so fenced regions are skipped.
	"""
	opens, closes = [], []
	fence = None  # (char, run length) of the currently open fence
	offset = 0
	for line in text.splitlines(keepends=True):
		stripped = line.lstrip()
		run = re.match(r"(`{3,}|~{3,})", stripped)
		if run:
			token = run.group(1)
			if fence is None:
				fence = (token[0], len(token))
			# CommonMark: only a run of the same character at least as long as
			# the opener closes a fence; a shorter run is fence content, so a
			# ``` line inside a ```` example must not end the example.
			elif fence[0] == token[0] and len(token) >= fence[1]:
				fence = None
		elif fence is None:
			if OPEN_RE.match(line.rstrip("\n")):
				opens.append(offset)
			elif CLOSE_RE.match(line.rstrip("\n")):
				closes.append((offset, offset + len(line.rstrip("\n"))))
		offset += len(line)
	return opens, closes


def find_block(text):
	"""Locate the managed block, refusing to guess when the markers are malformed.

	Returns (start, end) of the block including its trailing newline, or None if
	the file has no markers at all. Raises BlockError when the markers cannot be
	paired unambiguously -- an unclosed opener, a stray closer, or more than one
	block. Editing in those cases risks swallowing whatever sits between the
	markers, which is exactly the user content this tool must never touch.
	"""
	opens, closes = scan_markers(text)
	if not opens and not closes:
		return None
	if len(opens) != len(closes):
		raise BlockError(
			f"found {len(opens)} <leos-agent> opener(s) and {len(closes)} closer(s); "
			"fix the markers by hand, then re-run"
		)
	if len(opens) > 1:
		raise BlockError(
			f"found {len(opens)} <leos-agent> blocks; keep exactly one, then re-run"
		)
	start, end = opens[0], closes[0][1]
	if end < start:
		raise BlockError("the </leos-agent> closer appears before its opener; fix by hand")
	if end < len(text) and text[end] == "\n":
		end += 1
	return start, end


def inject(original, block):
	"""Replace the managed block, or append one. Returns the new file content."""
	span = find_block(original)
	if span:
		start, end = span
		return original[:start] + block + original[end:]
	if not original.strip():
		return block
	return original.rstrip("\n") + "\n\n" + block


def strip_block(original):
	span = find_block(original)
	if not span:
		return original
	start, end = span
	return original[:start] + original[end:]


def read_text(path):
	"""Read a file, remembering whether it used CRLF so a write can preserve it."""
	raw = path.read_bytes()
	text = raw.decode("utf-8")
	crlf = b"\r\n" in raw
	return (text.replace("\r\n", "\n"), crlf)


def default_mode():
	"""What a normally-created file would get, i.e. 0666 masked by the umask."""
	current = os.umask(0)
	os.umask(current)
	return 0o666 & ~current


def atomic_write(path, text, crlf):
	"""Write via a temp file in the same directory, then rename over the target.

	A plain write truncates first, so an interrupted run would leave the user's
	instruction file empty or half-written. The rename is atomic instead.
	"""
	from install_transaction import ACTIVE
	transaction = ACTIVE.get()
	if transaction is not None:
		data = (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")
		transaction.stage(path, data, path.stat().st_mode & 0o777 if path.is_file() else default_mode())
		return
	# Write through a symlink rather than over it: an instruction file symlinked
	# into a dotfiles repo must keep pointing there, and os.replace would
	# silently swap the link for a regular file.
	if path.is_symlink():
		path = Path(os.path.realpath(path))
	path.parent.mkdir(parents=True, exist_ok=True)
	data = (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")
	# mkstemp creates 0600; carry over the file's own mode so installing never
	# silently tightens (or loosens) the permissions the user had.
	mode = path.stat().st_mode & 0o777 if path.is_file() else default_mode()
	handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".leo-install-")
	tmp = Path(tmp_name)
	try:
		with os.fdopen(handle, "wb") as fh:
			fh.write(data)
			fh.flush()
			os.fsync(fh.fileno())
		os.chmod(tmp, mode)
		os.replace(tmp, path)
	except BaseException:
		tmp.unlink(missing_ok=True)
		raise


def unified(path, before, after):
	return "".join(
		difflib.unified_diff(
			before.splitlines(keepends=True),
			after.splitlines(keepends=True),
			fromfile=f"{path} (current)",
			tofile=f"{path} (new)",
		)
	)


def write_if_changed(path, new_text, current, existed, crlf, args, label):
	"""Write only when bytes differ. The idempotency guarantee lives here."""
	if existed and current == new_text:
		return Result(label, "unchanged")
	diff = unified(path, current, new_text) if args.dry_run else ""
	if args.writes:
		atomic_write(path, new_text, crlf)
	return Result(label, "updated" if existed else "created", diff=diff)


def install_markdown(path, block, args, label, create=True):
	"""Block-replace in a global instruction file."""
	path = path.expanduser()
	existed = path.is_file()
	if not existed and not create:
		return Result(label, "skipped", f"{path} not found")
	current, crlf = read_text(path) if existed else ("", False)

	if args.uninstall:
		if not existed or not find_block(current):
			return Result(label, "unchanged", "no block present")
		remainder = strip_block(current)
		if remainder.strip():
			remainder = remainder.rstrip("\n") + "\n"
			diff = unified(path, current, remainder) if args.dry_run else ""
			if args.writes:
				atomic_write(path, remainder, crlf)
			return Result(label, "removed", diff=diff)
		if args.writes:
			remove_file(path)
		return Result(label, "removed", "file held nothing else, deleted")

	return write_if_changed(path, inject(current, block), current, existed, crlf, args, label)


def migrate_legacy_block(path, args, label):
	"""Strip a <leos-agent> block a pre-payload-split version wrote into this
	harness's former global instruction file.

	The payload now arrives live from the plugin at session start, so nothing
	writes a block here any more -- but an install that ran before that
	change left one behind, and it will not clean itself up. Runs the same
	whether this is a normal install or an --uninstall: either way, a stale
	block should go, so there is only one code path instead of two.
	"""
	path = path.expanduser()
	if not path.is_file():
		return Result(label, "unchanged")
	current, crlf = read_text(path)
	if not find_block(current):
		return Result(label, "unchanged")
	remainder = strip_block(current)
	if not remainder.strip():
		if args.writes:
			remove_file(path)
		return Result(label, "migrated", "file held nothing else, deleted")
	remainder = remainder.rstrip("\n") + "\n"
	diff = unified(path, current, remainder) if args.dry_run else ""
	if args.writes:
		atomic_write(path, remainder, crlf)
	return Result(label, "migrated", diff=diff)


def opencode_config_advisory(root, home, label, configured=False):
	"""OpenCode reads its config as JSONC, comments and all -- rewriting it here
	would blow those away, so the most this tool can do is check whether the
	files are already wired into `instructions` and, when they are not, tell the
	user the line to add rather than adding it for them.

	Two paths, not one, once routing is configured: `instructions` reads
	preferences.md un-rendered, so the routing file beside it is the only way
	this machine's model choice reaches OpenCode at all.
	"""
	path = home / ".config" / "opencode" / "opencode.json"
	wanted = [str(root / "rules" / "preferences.md")]
	if configured:
		wanted.append(str(home / ".config" / "opencode" / "leos-agent-routing.md"))
	text = path.read_text(encoding="utf-8") if path.is_file() else ""
	missing = [p for p in wanted if p not in text]
	if not missing:
		return Result(label, "unchanged")
	listed = ", ".join(f'"{p}"' for p in wanted)
	return Result(label, "error", f'missing instructions: [{listed}]; run leo-install.py opencode')


def owned_copy(text):
	return (text.startswith("# Managed by leos-agent.") or text.startswith("# Installed by leos-agent.")
		or text.startswith("<!-- Managed by leos-agent. -->") or text.startswith("<!-- leos-agent -->")
		or text.startswith("---\n# Managed by leos-agent.\n"))



# The directories a plugin root sits directly above. A baked absolute path is
# recognised by the first of these that follows it, which is what makes the root
# recoverable from any reference shape -- quoted, backticked, or bare prose.
ROOT_DIRS = frozenset(("agents", "commands", "hooks", "payload", "rules", "scripts", "skills"))

# Paths are delimited by a quote, a backtick or a newline rather than by
# whitespace: a macOS root like "/Users/leo/Library/Application Support/leos-agent"
# contains a space and would otherwise be truncated to "/Users/leo/Library".
PATH_TOKEN = re.compile(r'/[^\n"\'`]+')


def candidate_roots(text, limit=64):
	"""Absolute paths in `text` that could be an older install's plugin root.

	Every split point is offered, not just the first: a root that itself contains
	a plugin directory name -- /opt/agents/leos-agent, ~/Library/skills/leos-agent --
	would otherwise resolve to /opt, and the copy would read as a stranger's file.
	Over-offering is free, because a full-content hash is still the arbiter.
	"""
	out = []
	for token in PATH_TOKEN.findall(text):
		parts = token.split("/")
		for index, part in enumerate(parts):
			# index > 1 keeps "/skills/..." itself from being read as a root.
			if part in ROOT_DIRS and index > 1:
				root = "/".join(parts[:index])
				if root and root not in out:
					out.append(root)
		if len(out) >= limit:
			return out[:limit]
	return out


def legacy_copy(text):
	"""Recognize unchanged pre-v12 copies by full content, not a loose marker."""
	import hashlib
	try:
		manifest = json.loads((Path(__file__).resolve().parents[1] / "payload/legacy-copy-hashes.json").read_text())
		expected = set(manifest["sha256"])
	except (OSError, ValueError, KeyError):
		return False
	# Older OpenCode installs replaced <plugin-root> with an absolute source path.
	# Undo that for each candidate root and require the entire known hash: a file
	# with a single edited line still fails every variant, which is the property
	# that lets this delete a copy without ever deleting someone's work.
	variants = {text}
	for root in candidate_roots(text):
		variants.add(text.replace(root, PLUGIN_ROOT_TOKEN))
	return any(hashlib.sha256(value.encode()).hexdigest() in expected for value in variants)


def remove_legacy_command(dest, args, label):
	if not dest.is_file():
		return Result(label, "unchanged")
	text = dest.read_text()
	if not owned_copy(text) and not legacy_copy(text):
		return Result(label, "unchanged", "preserved unrelated or edited command")
	if args.writes:
		remove_file(dest)
	return Result(label, "removed", "native skill replaces duplicate command")


def install_file_copy(src, dest, args, label, owned_parent=False, payload=None):
	"""Install a payload file the harness's plugin system cannot deliver itself.

	`payload` overrides the source text for files rendered from the machine's
	routing config rather than copied verbatim.
	"""
	dest = dest.expanduser()
	existed = dest.is_file()
	if payload is None:
		payload = src.read_text(encoding="utf-8")
	current = dest.read_text(encoding="utf-8") if existed else ""

	# Never clobber or delete a same-named file this tool did not put there.
	foreign = existed and current != payload and not owned_copy(current) and not legacy_copy(current)
	if foreign and not args.force:
		return Result(label, "conflict", "a file we did not write is already here; re-run with --force to replace it")

	if args.uninstall:
		if not existed:
			return Result(label, "unchanged", "not present")
		if args.writes:
			remove_file(dest)
			if owned_parent and not transaction_active() and dest.parent.is_dir() and not any(dest.parent.iterdir()):
				dest.parent.rmdir()
		return Result(label, "removed")

	return write_if_changed(dest, payload, current, existed, False, args, label)


def opencode_payload(src, root, rename_install=False):
	"""An OpenCode copy's content: the plugin root baked in, optionally renamed.

	OpenCode reads the copies out of ~/.config/opencode, far from the scripts
	they invoke, and sets none of the resolution env vars — so the placeholder
	is resolved here, at install time, where the root is known for certain.
	The install skill is additionally renamed to match the leo-install/
	directory it is copied into, keeping directory and frontmatter in
	agreement whichever one OpenCode keys on.
	"""
	text = src.read_text(encoding="utf-8").replace(PLUGIN_ROOT_TOKEN, str(root))
	if rename_install:
		text = re.sub(r"(?m)^name:\s*install\s*$", "name: leo-install", text, count=1)
	if text.startswith("---\n"):
		text = text.replace("---\n", "---\n# Managed by leos-agent.\n", 1)
	else:
		text = "<!-- Managed by leos-agent. -->\n" + text
	return text


def _run_targets(harness, root, args):
	# Read once per run: routing.load() is used by Codex's TOML rendering and
	# Cursor's rule, both still per-machine even though the payload is not.
	config = routing.load()
	home = Path.home()
	harness_dir = config_dir(harness, home)
	targets = []

	if harness == "claude":
		label = "~/.claude/CLAUDE.md"
		targets.append((label, lambda: migrate_legacy_block(harness_dir / "CLAUDE.md", args, label)))

	elif harness == "codex":
		label = "~/.codex/AGENTS.md"
		targets.append((label, lambda: migrate_legacy_block(harness_dir / "AGENTS.md", args, label)))
		for agent_name in CODEX_AGENTS:
			label = f"~/.codex/agents/{agent_name}.toml"
			targets.append(
				(
					label,
					lambda n=agent_name, l=label: install_file_copy(
						root / "payload" / "codex-agents" / f"{n}.toml",
						harness_dir / "agents" / f"{n}.toml",
						args,
						l,
						payload=render_codex_agent(
							(root / "payload" / "codex-agents" / f"{n}.toml").read_text(encoding="utf-8"),
							n,
							config,
						),
					),
				)
			)

	elif harness == "cursor":
		# Global native agents are supported; global ~/.cursor/rules is not.
		legacy = harness_dir / "rules" / "leos-agent-routing.mdc"
		if legacy.is_file() and "leos-agent model routing" in legacy.read_text():
			def remove_legacy():
				if args.writes:
					remove_file(legacy)
				return Result(str(legacy), "removed", "obsolete global routing rule")
			targets.append((str(legacy), remove_legacy))
		for name in CODEX_AGENTS:
			dest = harness_dir / "agents" / (name + ".md")
			targets.append((str(dest), lambda n=name, d=dest: install_file_copy(
				None, d, args, str(d), payload=native_agent(root, n, harness, config))))

	elif harness == "hermes":
		# Hermes writes its own starter identity file on first run and needs
		# nothing installed into it; the only leftover job is taking back a
		# block an earlier version wrote before it existed.
		label = "~/.hermes/SOUL.md"
		targets.append((label, lambda: migrate_legacy_block(harness_dir / "SOUL.md", args, label)))

	elif harness == "pi":
		label = "~/.pi/agent/AGENTS.md"
		targets.append((label, lambda: migrate_legacy_block(harness_dir / "AGENTS.md", args, label)))

	elif harness == "opencode":
		cfg = harness_dir
		label = "~/.config/opencode/AGENTS.md"
		targets.append((label, lambda: migrate_legacy_block(cfg / "AGENTS.md", args, label)))
		routing_label = "~/.config/opencode/leos-agent-routing.md"
		routing_dest = cfg / "leos-agent-routing.md"
		targets.append((routing_label, lambda: install_file_copy(None, routing_dest, args, routing_label,
			payload="<!-- Managed by leos-agent. -->\n" + payload_body(root, harness, config) + "\n")))
		json_label = "~/.config/opencode/opencode.json"
		targets.append((json_label, lambda: manage_opencode_config(root, cfg, args, json_label)))
		for name in CODEX_AGENTS:
			dest = cfg / "agents" / (name + ".md")
			targets.append((str(dest), lambda n=name, d=dest: install_file_copy(
				None, d, args, str(d), payload=native_agent(root, n, harness, config))))
		# OpenCode plugins cannot register skills or commands from JS, so the
		# payload files are copied into the config dir where it reads them.
		skill_label = "~/.config/opencode/skills/leo-install/SKILL.md"
		targets.append(
			(
				skill_label,
				lambda: install_file_copy(
					root / "skills" / "install" / "SKILL.md",
					cfg / "skills" / "leo-install" / "SKILL.md",
					args,
					skill_label,
					owned_parent=True,
					payload=opencode_payload(
						root / "skills" / "install" / "SKILL.md", root, rename_install=True
					),
				),
			)
		)
		# The remaining skills and their commands, same reason. Bound late via a
		# default argument: a lambda closing over the loop variable would copy
		# the last entry every time.
		for skill_name in OPENCODE_SKILLS:
			# reference/ files first, so their directory is gone by the time the
			# SKILL.md target tries to remove the now-empty skill directory.
			for ref in sorted((root / "skills" / skill_name / "reference").glob("*.md")):
				ref_label = f"~/.config/opencode/skills/{skill_name}/reference/{ref.name}"
				targets.append(
					(
						ref_label,
						lambda s=ref, n=skill_name, l=ref_label: install_file_copy(
							s,
							cfg / "skills" / n / "reference" / s.name,
							args,
							l,
							owned_parent=True,
							payload=opencode_payload(s, root),
						),
					)
				)
			extra_label = f"~/.config/opencode/skills/{skill_name}/SKILL.md"
			targets.append(
				(
					extra_label,
					lambda n=skill_name, l=extra_label: install_file_copy(
						root / "skills" / n / "SKILL.md",
						cfg / "skills" / n / "SKILL.md",
						args,
						l,
						owned_parent=True,
						payload=opencode_payload(root / "skills" / n / "SKILL.md", root),
					),
				)
			)
		for command_name in OPENCODE_COMMANDS:
			command_label = f"{cfg}/commands/{command_name}.md"
			targets.append((command_label, lambda n=command_name, l=command_label:
				remove_legacy_command(cfg / "commands" / f"{n}.md", args, l)))

	# Each target reports on its own. One failure must not discard the report
	# for the others, or hide what already landed.
	results = []
	for label, target in targets:
		try:
			results.append(target())
		except (BlockError, ValueError) as exc:
			results.append(Result(label, "error", str(exc)))
		except UnicodeDecodeError:
			results.append(Result(label, "error", "not valid UTF-8 text; refusing to rewrite it"))
		except OSError as exc:
			results.append(Result(label, "error", exc.strerror or str(exc)))
	return results


def config_dir(harness, home=None):
	home = home or Path.home()
	defaults = {"claude": home / ".claude", "codex": home / ".codex",
		"cursor": home / ".cursor", "hermes": home / ".hermes", "pi": home / ".pi" / "agent",
		"opencode": Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))) / "opencode"}
	variables = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "hermes": "HERMES_HOME",
		"pi": "PI_CODING_AGENT_DIR", "opencode": "OPENCODE_CONFIG_DIR"}
	value = os.environ.get(variables.get(harness, "LEOS_AGENT_UNUSED_CONFIG_DIR"))
	return Path(value).expanduser() if value else defaults[harness]


def transaction_active():
	from install_transaction import ACTIVE
	return ACTIVE.get() is not None


def remove_file(path):
	from install_transaction import ACTIVE
	transaction = ACTIVE.get()
	if transaction is not None:
		transaction.stage(path, None)
	else:
		path.unlink()


def native_agent(root, name, harness, config):
	from routing_engine import tier_for
	text = (root / "agents" / (name + ".md")).read_text()
	_, frontmatter, body = text.split("---", 2)
	description = re.search(r"(?m)^description: (.+)$", frontmatter).group(1)
	tier = tier_for(name)
	model = routing.tier_model(harness, tier, config)
	fields = "---\n# Managed by leos-agent.\nname: " + name + "\ndescription: " + json.dumps(description) + "\n"
	if harness == "opencode":
		fields += "mode: subagent\n"
		# The reviewer delegates bounded lenses under review-pr; ordinary workers
		# never delegate, and OpenCode can enforce that per agent.
		if name != "leo-reviewer":
			fields += "tools:\n  task: false\n"
	# Omit the key rather than inventing a value. "inherit" is Claude Code's
	# frontmatter, not a model identifier Cursor would resolve, and writing it
	# claimed a routing decision no harness was making. Unconfigured now means the
	# harness's own default on Cursor and OpenCode alike.
	if model:
		fields += "model: " + json.dumps(model) + "\n"
	return fields + "---\n" + body


def manage_opencode_config(root, cfg, args, label):
	from jsonc_edit import update_array, properties
	path = Path(os.environ["OPENCODE_CONFIG"]).expanduser() if os.environ.get("OPENCODE_CONFIG") else (
		cfg / "opencode.jsonc" if (cfg / "opencode.jsonc").exists() else cfg / "opencode.json")
	receipt = cfg / "leos-agent-paths.json"
	previous = json.loads(receipt.read_text()) if receipt.exists() else {}
	current = path.read_text() if path.exists() else "{}\n"
	_, spans, _ = properties(current)
	wanted = {"instructions": [str(cfg / "leos-agent-routing.md")], "plugin": [(root / "index.js").resolve().as_uri()]}
	updated = current
	for key, additions in wanted.items():
		remove = list(previous.get(key, []))
		values = spans.get(key, (None, None, []))[2]
		if not isinstance(values, list):
			raise ValueError(f"{key} must be an array")
		for value in values:
			if not isinstance(value, str):
				continue
			if key == "plugin" and (value == "leos-agent" or value.startswith("leos-agent@")):
				remove.append(value)
			if key == "instructions" and value == str(root / "rules" / "preferences.md"):
				remove.append(value)
		# Do not remove/re-add unchanged entries: idempotency includes bytes.
		remove = [v for v in remove if args.uninstall or v not in additions]
		updated = update_array(updated, key, [] if args.uninstall else additions, remove)
	if args.writes:
		if current != updated:
			atomic_write(path, updated, False)
		if args.uninstall:
			if receipt.exists():
				remove_file(receipt)
		else:
			atomic_write(receipt, json.dumps(wanted, indent=2) + "\n", False)
	return Result(label, "unchanged" if current == updated else "updated" if path.exists() else "created")


def run(harness, root, args):
	from install_transaction import ACTIVE, Transaction
	tx = Transaction(config_dir(harness) / "leos-agent-install-backup.json")
	token = ACTIVE.set(tx) if args.writes else None
	try:
		results = _run_targets(harness, root, args)
		if args.writes:
			if any(result.failed for result in results):
				for result in results:
					if result.changed:
						result.status = "skipped"
						result.detail = "transaction aborted; another target needs attention"
			else:
				tx.commit()
				# Remove only empty directories left by deleted owned files.
				boundary = config_dir(harness).resolve()
				for path, (_, after, _) in tx.changes.items():
					if after is not None:
						continue
					parent = path.parent
					while parent != boundary and boundary in parent.parents:
						try:
							parent.rmdir()
						except OSError:
							break
						parent = parent.parent
		return results
	except (ValueError, OSError) as exc:
		return [Result(harness, "error", str(exc))]
	finally:
		if token is not None:
			ACTIVE.reset(token)


def main(argv=None):
	parser = argparse.ArgumentParser(
		prog="leo-install.py",
		description=(
			"Install what ONE harness's plugin system cannot deliver on its own, "
			"and clean up any block an earlier version left in its global instruction file."
		),
	)
	parser.add_argument("harness", choices=HARNESSES, help="the harness this session is running in")
	mode = parser.add_mutually_exclusive_group()
	mode.add_argument("--dry-run", action="store_true", help="show diffs, write nothing")
	mode.add_argument(
		"--uninstall", action="store_true", help="remove any installed payload files and legacy blocks"
	)
	mode.add_argument("--check", action="store_true", help="exit 1 if anything would change")
	mode.add_argument("--rollback", action="store_true", help="restore the previous installation if files have not since changed")
	parser.add_argument("--force", action="store_true", help="replace a conflicting file this tool did not write")
	args = parser.parse_args(argv)
	args.writes = not (args.dry_run or args.check)
	if args.rollback:
		from install_transaction import rollback
		try:
			print(f"restored {rollback(config_dir(args.harness) / 'leos-agent-install-backup.json')} files")
			return 0
		except (ValueError, OSError) as exc:
			print(f"rollback refused: {exc}", file=sys.stderr)
			return 1

	root = plugin_root()
	if not (root / "rules" / "preferences.md").is_file():
		sys.exit(f"leo-install: cannot find the plugin payload from {root}; set LEOS_AGENT_ROOT")

	version = read_version(root)
	results = run(args.harness, root, args)

	if args.uninstall:
		verb = "removing"
	elif args.writes:
		verb = "installing"
	else:
		verb = "would install"
	print(f"leos-agent {version} — {verb} {args.harness}")
	for res in results:
		print(res.line(pending=not args.writes))
		if res.diff:
			print("".join(f"    {ln}" for ln in res.diff.splitlines(keepends=True)))

	changed = [r for r in results if r.changed]
	failed = [r for r in results if r.failed]

	if failed:
		print(f"\n{len(failed)} target(s) could not be handled; nothing further was attempted for them")
		return 1
	if args.check:
		if changed:
			print(f"\n{len(changed)} target(s) out of date; run leo-install.py {args.harness}")
			return 1
		print("\nup to date")
		return 0
	if args.dry_run:
		print(f"\n{len(changed)} target(s) would change; nothing written")
	else:
		print(f"\n{len(changed)} target(s) changed")
		if not args.uninstall and os.environ.get("LEOS_AGENT_PRICE_REFRESH") != "off":
			import pricing
			pricing.refresh_background(force=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())
