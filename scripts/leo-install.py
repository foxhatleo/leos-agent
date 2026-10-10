#!/usr/bin/env python3
"""Install what a harness's plugin system cannot deliver on its own.

The payload itself lives in rules/preferences.md and is read live by each
harness's plugin (a SessionStart hook, or Cursor's alwaysApply rule) at
session start, so this tool no longer writes it into any global instruction
file. What is left to install is per-harness and small: Codex's agent
profiles (its plugins cannot ship custom agent definitions), Cursor's native
user agents, and OpenCode's agents plus skill and reference copies with this
machine's plugin root baked in. It also cleans up the <leos-agent> block an
earlier version of this tool left in a harness's former global file, now that
nothing writes there.

Acts on exactly ONE harness per run -- the one named on the command line. A
session running in Codex installs Codex and nothing else. The harness's config
directory must already exist; this tool never creates one.

Ownership: every file this tool writes is recorded, with the sha256 of its
bytes, in a receipt (leos-agent-paths.json) in that config directory. A file is
replaced or deleted only while its bytes still match that receipt, the frozen
hashes of earlier releases' copies, or this release's own copy. A header saying
"Managed by leos-agent" is never enough on its own.

Usage:
    leo-install.py <harness> [--dry-run | --uninstall | --check | --rollback] [--force]

Harnesses: claude, codex, cursor, hermes, pi, opencode
"""

import argparse
import difflib
import hashlib
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

# Every copied payload file carries this string. Older releases decided
# ownership by it alone; it now only tells an edited old copy (preserved) apart
# from a file that never came from this plugin (a conflict).
PROVENANCE = "leos-agent"

# Skill and command text refers to scripts as <plugin-root>/scripts/… and the
# model resolves the root once. OpenCode copies live apart from the scripts,
# and OpenCode sets no resolution env var, so their copies are installed with
# the token already replaced by this machine's absolute plugin root. Installing
# alternately from a checkout and a cache re-bakes the root each time — last
# install wins, and --check reports a stale root as out of date.
PLUGIN_ROOT_TOKEN = "<plugin-root>"

# OpenCode skills are copied to disk with the plugin root baked in (see
# PLUGIN_ROOT_TOKEN). check.py asserts every file they name carries PROVENANCE.
OPENCODE_SKILLS = ("doctor", "review-pr", "handoff", "handon", "tune-routing", "review-usage")
OPENCODE_COMMANDS = ("review-pr", "handoff", "handon")

# Codex plugins cannot package custom agent definitions directly, so these are
# copied into ~/.codex/agents. Keep this tuple authoritative: check.py and the
# installer tests derive the expected payload from it.
CODEX_AGENTS = ("leo-cheap", "leo-standard", "leo-premium", "leo-parent", "leo-reviewer", "leo-lens")

# Profiles earlier releases installed and this one no longer ships. Their copies
# are taken back on receipt or full-content evidence only, and their sources may
# be gone from the plugin tree; anything else at those paths is preserved.
RETIRED_AGENTS = ("leo-runner", "leo-executor")

# The per-config-directory receipt. OpenCode's registration fields predate the
# per-file hashes and keep their top-level keys.
RECEIPT = "leos-agent-paths.json"
# Where releases before receipts kept the installation backup. Read for
# rollback and as ownership evidence, then moved to the data directory.
LEGACY_BACKUP = "leos-agent-install-backup.json"
COPYING = ("codex", "cursor", "opencode")

# Native config-directory overrides, and how to make each directory exist.
OVERRIDES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "hermes": "HERMES_HOME",
	"pi": "PI_CODING_AGENT_DIR", "opencode": "OPENCODE_CONFIG_DIR"}
SETUP_HINTS = {
	"claude": "run Claude Code once, or set CLAUDE_CONFIG_DIR to its config directory",
	"codex": "run Codex once, or set CODEX_HOME to its config directory",
	"cursor": "open Cursor once so it creates ~/.cursor",
	"hermes": "run Hermes once, or set HERMES_HOME to its home directory",
	"pi": "run Pi once, or set PI_CODING_AGENT_DIR to its agent directory",
	"opencode": "run OpenCode once, or set OPENCODE_CONFIG_DIR (or XDG_CONFIG_HOME) to its config location",
}


class BlockError(Exception):
	"""The target file's markers are malformed; editing it could destroy content."""


class ConfigError(ValueError):
	"""A harness config location is missing or an override is unusable."""


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


def render_codex_agent(text):
	"""Profiles carry instructions; the dispatch guard enforces model selection."""
	return re.sub(r'(?m)^model(?:_reasoning_effort)? = .*\n', "", text)


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


def encode(text, crlf=False):
	return (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")


def atomic_write(path, text, crlf):
	"""Write via a temp file in the same directory, then rename over the target.

	A plain write truncates first, so an interrupted run would leave the user's
	instruction file empty or half-written. The rename is atomic instead.
	"""
	from install_transaction import ACTIVE
	transaction = ACTIVE.get()
	data = encode(text, crlf)
	if transaction is not None:
		transaction.stage(path, data, path.stat().st_mode & 0o777 if path.is_file() else default_mode())
		return
	# Write through a symlink rather than over it: an instruction file symlinked
	# into a dotfiles repo must keep pointing there, and os.replace would
	# silently swap the link for a regular file.
	if path.is_symlink():
		path = Path(os.path.realpath(path))
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
	if not path.parent.is_dir():
		return Result(label, "unchanged", f"{display(path.parent)} does not exist; nothing to migrate")
	if not path.is_file():
		return Result(label, "unchanged")
	current, crlf = read_text(path)
	if not find_block(current):
		return Result(label, "unchanged")
	remainder = strip_block(current)
	if not remainder.strip():
		if path.is_symlink():
			# The link is the user's (a dotfiles checkout, typically). Deleting it
			# would leave the block in the target; empty the target instead.
			diff = unified(path, current, "") if args.dry_run else ""
			if args.writes:
				atomic_write(path, "", crlf)
			return Result(label, "migrated", "file held nothing else; emptied through its symlink", diff=diff)
		if args.writes:
			remove_file(path)
		return Result(label, "migrated", "file held nothing else, deleted")
	remainder = remainder.rstrip("\n") + "\n"
	diff = unified(path, current, remainder) if args.dry_run else ""
	if args.writes:
		atomic_write(path, remainder, crlf)
	return Result(label, "migrated", diff=diff)


def owned_copy(text):
	"""True when `text` starts with a header this installer writes.

	A header is provenance, not ownership: anyone can type it, and an edited copy
	keeps it. Ownership is decided by `ownership()`.
	"""
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


_KNOWN = {}


def known_hashes(kind):
	"""Frozen full-content hashes: "sha256" for pre-v12 copies, "released" for
	every copy a release wrote before receipts existed (see strip_routed_model)."""
	if not _KNOWN:
		try:
			manifest = json.loads((Path(__file__).resolve().parents[1] / "payload/legacy-copy-hashes.json").read_text())
			_KNOWN["sha256"] = frozenset(manifest["sha256"])
			_KNOWN["released"] = frozenset(manifest.get("released", {}).get("sha256", ()))
		except (OSError, ValueError, KeyError, AttributeError):
			_KNOWN.update(sha256=frozenset(), released=frozenset())
	return _KNOWN[kind]


def root_variants(text):
	"""`text`, plus a copy with each candidate baked root turned back into the token."""
	variants = {text}
	for root in candidate_roots(text):
		variants.add(text.replace(root, PLUGIN_ROOT_TOKEN))
	return variants


def strip_routed_model(text, model=None):
	"""Drop the one frontmatter `model:` line a release derived from routing config.

	Native agent copies carry the configured model, or "inherit" in early
	releases, so their bytes differ per machine. The line goes only when it holds
	"inherit" or exactly the model this machine's config names for the agent: a
	hand-edited model is a user edit and keeps the copy out of the frozen hashes.
	"""
	if not text.startswith("---\n"):
		return text
	end = text.find("\n---\n", 3)
	if end < 0:
		return text
	accepted = {"inherit", json.dumps("inherit")}
	if model:
		accepted.add(json.dumps(model))
	lines = text[:end + 1].splitlines(keepends=True)
	for index, line in enumerate(lines):
		match = re.fullmatch(r"model: (.*)\n", line)
		if match and match.group(1) in accepted:
			return "".join(lines[:index] + lines[index + 1:]) + text[end + 1:]
	return text


def digest(data):
	return hashlib.sha256(data).hexdigest()


def known_copy(text, model=None):
	"""Full-content match against any copy an earlier release wrote.

	Older OpenCode installs replaced <plugin-root> with an absolute source path.
	Undo that for each candidate root and require the entire known hash: a file
	with a single edited line still fails every variant, which is the property
	that lets this delete a copy without ever deleting someone's work.
	"""
	expected = known_hashes("sha256") | known_hashes("released")
	for value in root_variants(text):
		for variant in {value, strip_routed_model(value, model)}:
			if digest(variant.encode()) in expected:
				return True
	return False


class Receipt:
	"""What this installer wrote into one harness config directory.

	`files` maps a path relative to the config directory to the sha256 of the
	bytes written there. OpenCode's registration (which config file, which
	entries, which keys and whether the file itself were created) sits beside it.
	"""

	def __init__(self, cfg, harness):
		self.cfg = cfg
		self.path = cfg / RECEIPT
		self.data = {}
		if self.path.is_file():
			try:
				data = json.loads(self.path.read_text(encoding="utf-8"))
			except ValueError:
				raise ValueError(f"{display(self.path)} is not valid JSON; fix or delete it, then re-run")
			if not isinstance(data, dict):
				raise ValueError(f"{display(self.path)} is not a leos-agent receipt; fix or delete it, then re-run")
			self.data = data
		files = self.data.get("files")
		self.files = {k: v for k, v in files.items() if isinstance(v, str)} if isinstance(files, dict) else {}
		# Receipts arrived after releases that wrote copies without one. For those
		# installs, the last installation backup is the only record of the bytes
		# written, so its after-hashes count as receipts until one exists.
		self.pre_receipt = not isinstance(files, dict)
		self.backup = {}
		if self.pre_receipt:
			for candidate in (backup_path(harness), cfg / LEGACY_BACKUP):
				try:
					entries = json.loads(candidate.read_text()).get("files", [])
					self.backup.update((e["path"], e["after_sha256"]) for e in entries
						if isinstance(e, dict) and isinstance(e.get("path"), str))
				except (OSError, ValueError, AttributeError, KeyError, TypeError, ConfigError):
					continue
		self.written = {}
		self.covered = set()
		self.meta = None

	def key(self, path):
		return Path(os.path.relpath(path, self.cfg)).as_posix()

	def recorded(self, path):
		return self.files.get(self.key(path))

	def record(self, path, text):
		self.written[self.key(path)] = digest(encode(text))

	def evidence(self, path, raw, text):
		"""True when a receipt-era or pre-receipt record says these bytes are ours."""
		if self.recorded(path) == digest(raw):
			return True
		if not self.pre_receipt:
			return False
		if self.backup.get(str(Path(path).resolve())) == digest(raw):
			return True
		# The OpenCode receipt listed the rendered routing rule before per-file
		# hashes existed. Its body is rendered from this machine's routing config,
		# so no frozen hash can cover it; the registration is the receipt.
		listed = self.data.get("instructions")
		return isinstance(listed, list) and str(path) in listed and owned_copy(text)

	def render(self):
		data = {"schema": 1, "files": dict(sorted(self.written.items()))}
		if self.meta:
			data.update(self.meta)
		return json.dumps(data, indent=2) + "\n"


def ownership(path, raw, receipt=None, expected=None, model=None):
	"""Who wrote `path`, judged from its bytes.

	"ours": the bytes match a receipt, an earlier release's copy, or this
	release's own copy. "edited": it carries our header or receipt but its bytes
	changed. "legacy": an older release's copy (provenance, no header) changed
	since. "foreign": nothing says leos-agent wrote it.
	"""
	text = raw.decode("utf-8")
	if expected is not None and text == expected:
		return "ours"
	if receipt is not None and receipt.evidence(path, raw, text):
		return "ours"
	if known_copy(text, model):
		return "ours"
	if owned_copy(text) or (receipt is not None and receipt.recorded(path) is not None):
		return "edited"
	if PROVENANCE in text:
		return "legacy"
	return "foreign"


KEPT = {
	"edited": "edited since leos-agent wrote it",
	"legacy": "an older leos-agent copy, edited since",
	"foreign": "leos-agent did not write it",
}


def install_file_copy(dest, payload, args, receipt=None, label=None, model=None):
	"""Install (or, on --uninstall, take back) one payload copy.

	A copy is replaced or removed only when `ownership()` says it is ours.
	Install: an edited or foreign file at the path is a conflict (--force
	replaces it); an edited copy from an older release is preserved and the rest
	of the install continues. Uninstall: anything not ours is preserved, and
	--force does not change that.
	"""
	label = label or display(dest)
	existed = dest.is_file()
	if dest.exists() and not existed:
		return Result(label, "error", "not a regular file; refusing to replace it")
	if args.uninstall:
		if not existed:
			return Result(label, "unchanged", "not present")
		try:
			expected = payload() if callable(payload) else payload
		except (OSError, ValueError):
			expected = None  # this release's copy cannot be rendered; other evidence decides
		state = ownership(dest, dest.read_bytes(), receipt, expected=expected, model=model)
		if state != "ours":
			return Result(label, "preserved", KEPT[state])
		if args.writes:
			remove_file(dest)
		return Result(label, "removed")
	if callable(payload):
		payload = payload()
	current = ""
	if existed:
		raw = dest.read_bytes()
		current = raw.decode("utf-8")
		if current != payload:
			state = ownership(dest, raw, receipt, model=model)
			if state == "legacy" and not args.force:
				return Result(label, "preserved", KEPT[state] + "; delete it, or re-run with --force, to take this release's copy")
			if state in ("edited", "foreign") and not args.force:
				return Result(label, "conflict", KEPT[state] + "; move your changes elsewhere, or re-run with --force to replace it")
	if receipt is not None:
		receipt.record(dest, payload)
	return write_if_changed(dest, payload, current, existed, False, args, label)


def retire_copy(dest, args, receipt=None, label=None, expected=None, model=None, why="retired profile"):
	"""Remove a copy an earlier release installed and this one does not ship.

	Runs the same on install and uninstall. Only provably unchanged copies go;
	the rest are preserved whatever --force says.
	"""
	label = label or display(dest)
	if not dest.is_file():
		return Result(label, "unchanged")
	if callable(expected):
		expected = expected()
	state = ownership(dest, dest.read_bytes(), receipt, expected=expected, model=model)
	if state != "ours":
		return Result(label, "preserved", f"{why}: {KEPT[state]}")
	if args.writes:
		remove_file(dest)
	return Result(label, "removed", why)


def remove_legacy_command(dest, args, label, receipt=None):
	if not dest.is_file():
		return Result(label, "unchanged")
	state = ownership(dest, dest.read_bytes(), receipt)
	if state != "ours":
		return Result(label, "preserved", f"old command wrapper: {KEPT[state]}")
	if args.writes:
		remove_file(dest)
	return Result(label, "removed", "native skill replaces duplicate command")


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


def display(path):
	"""The path as the report shows it: ~-relative when under $HOME."""
	path = Path(path)
	try:
		return "~/" + path.relative_to(Path.home()).as_posix()
	except ValueError:
		return str(path)


def override(name):
	"""An override variable's path, or None when unset or empty.

	An empty value means unset, as it does to the harnesses. A relative value is
	refused: it would resolve against whatever directory the installer happened
	to be started from.
	"""
	value = os.environ.get(name)
	if not value:
		return None
	path = Path(value).expanduser()
	if not path.is_absolute():
		raise ConfigError(f"{name}={value!r} is not an absolute path; set it to one, or unset it")
	return path


def config_dir(harness, home=None):
	home = home or Path.home()
	explicit = override(OVERRIDES[harness]) if harness in OVERRIDES else None
	if explicit is not None:
		return explicit
	if harness == "opencode":
		return (override("XDG_CONFIG_HOME") or home / ".config") / "opencode"
	return {"claude": home / ".claude", "codex": home / ".codex", "cursor": home / ".cursor",
		"hermes": home / ".hermes", "pi": home / ".pi" / "agent"}[harness]


def require_config_dir(harness, cfg):
	"""Refuse to write copies into a config directory that does not exist.

	A missing directory usually means a mistyped override or a harness never run
	here, and creating it would make that look like success. Claude, Hermes and Pi
	get no copies; with no directory there is simply no legacy block to migrate.
	"""
	if harness in COPYING and not cfg.is_dir():
		raise ConfigError(f"{display(cfg)} does not exist, so {harness} is not configured here; "
			f"{SETUP_HINTS[harness]}. leo-install never creates a harness config directory")


def backup_path(harness):
	"""The installation backup, kept in the data directory: config directories
	often live in dotfiles repositories, and a backup can hold secrets."""
	import state
	root = Path(state._data_root()).expanduser()
	if not root.is_absolute():
		raise ConfigError(f"LEOS_AGENT_LOCAL_PATH={str(root)!r} is not an absolute path; set it to one, or unset it")
	return root / "install-backups" / f"{harness}.json"


def prepare_backup_dir(backup):
	root = backup.parent.parent
	if not root.is_dir():
		os.makedirs(root, mode=0o700, exist_ok=True)
	os.makedirs(backup.parent, mode=0o700, exist_ok=True)
	os.chmod(backup.parent, 0o700)


def remove_file(path):
	from install_transaction import ACTIVE
	transaction = ACTIVE.get()
	if transaction is not None:
		transaction.stage(path, None)
	else:
		path.unlink()


def agent_model(name, harness, config):
	"""The model this machine's routing config renders into `name`, if any."""
	try:
		from routing_engine import tier_for
		tier = tier_for(name)
		return routing.tier_model(harness, tier, config) if tier else None
	except (ValueError, KeyError, TypeError):
		return None


def native_agent(root, name, harness, config):
	text = (root / "agents" / (name + ".md")).read_text()
	_, frontmatter, body = text.split("---", 2)
	description = re.search(r"(?m)^description: (.+)$", frontmatter).group(1)
	model = agent_model(name, harness, config)
	# A Claude profile that denies Edit and Write (leo-lens) is read-only; render
	# that with each harness's own control rather than as prose alone.
	denied = re.search(r"(?m)^disallowedTools:(.*)$", frontmatter)
	read_only = bool(denied) and {"Edit", "Write"} <= {tool.strip() for tool in denied.group(1).split(",")}
	fields = "---\n# Managed by leos-agent.\nname: " + name + "\ndescription: " + json.dumps(description) + "\n"
	if harness == "opencode":
		fields += "mode: subagent\n"
		# OpenCode's `steps` caps an agent's iterations, then disables tools so
		# it answers in text: the native form of the profile's Claude maxTurns.
		# Cursor has no per-agent cap, so its copy carries neither key.
		turns = re.search(r"(?m)^maxTurns: *([1-9][0-9]*) *$", frontmatter)
		if turns:
			fields += "steps: " + turns.group(1) + "\n"
		# The reviewer delegates bounded lenses under review-pr; ordinary workers
		# never delegate, and OpenCode can enforce that per agent.
		if name != "leo-reviewer":
			fields += "permission:\n  task: deny\n"
			# OpenCode's edit permission covers its edit, write and apply_patch tools.
			if read_only:
				fields += "  edit: deny\n"
	elif harness == "cursor" and read_only:
		fields += "readonly: true\n"
	# Omit the key rather than inventing a value. "inherit" is Claude Code's
	# frontmatter, not a model identifier Cursor would resolve, and writing it
	# claimed a routing decision no harness was making. Unconfigured now means the
	# harness's own default on Cursor and OpenCode alike.
	if model:
		fields += "model: " + json.dumps(model) + "\n"
	return fields + "---\n" + body


def opencode_config_path(cfg):
	explicit = override("OPENCODE_CONFIG")
	if explicit is not None:
		if not explicit.parent.is_dir():
			raise ConfigError(f"OPENCODE_CONFIG points into {explicit.parent}, which does not exist; "
				"leo-install will not create it")
		return explicit
	jsonc = cfg / "opencode.jsonc"
	return jsonc if jsonc.exists() else cfg / "opencode.json"


def opencode_cache_spec(root):
	"""The package spec OpenCode installed this root from, when it is OpenCode's own
	npm cache (<cache>/opencode/packages/<spec>/node_modules/leos-agent).

	A file:// link into that cache breaks when OpenCode cleans or re-resolves it,
	while the spec itself makes OpenCode reinstall the package there.
	"""
	parts = Path(root).parts
	if len(parts) >= 5 and parts[-1] == PROVENANCE and parts[-2] == "node_modules" and parts[-4] == "packages" \
		and parts[-5] == "opencode":
		spec = parts[-3]
		if spec == PROVENANCE or spec.startswith(PROVENANCE + "@"):
			return spec
	return None


def _strings(value):
	return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def edit_opencode_config(path, root, args, wanted, ours, created_keys, created_file, note=""):
	"""Register `wanted` in one OpenCode config file and take back `ours`.

	Returns (result, registration) where registration is what the receipt keeps
	about this file: the entries this installer added (not ones the user already
	had), the keys it created, and whether it created the file itself.
	"""
	from jsonc_edit import drop_empty_array, is_empty, properties, update_array
	label = display(path)
	existed = path.is_file()
	if not existed and not wanted:
		return Result(label, "unchanged", "not present"), None
	current, crlf = read_text(path) if existed else ("{}\n", False)
	_, spans, _ = properties(current)
	# Older receipts did not record key ownership. Preserve those keys rather
	# than guessing that an empty user setting belonged to this installer.
	created = set(created_keys) | {key for key in wanted if key not in spans}
	created_file = created_file or not existed
	updated = current
	registration = {}
	for key in ("instructions", "plugin"):
		values = spans.get(key, (None, None, []))[2]
		if not isinstance(values, list):
			raise ValueError(f"{label}: {key} must be an array")
		additions = wanted.get(key, [])
		remove = [v for v in ours.get(key, []) if v not in additions]
		for value in values:
			if not isinstance(value, str):
				continue
			# An npm spec would load the plugin a second time beside the link.
			if key == "plugin" and wanted and additions and additions[0].startswith("file:") and (
				value == PROVENANCE or value.startswith(PROVENANCE + "@")):
				remove.append(value)
			if key == "instructions" and value == str(root / "rules" / "preferences.md"):
				remove.append(value)
		updated = update_array(updated, key, additions, remove)
		registration[key] = [v for v in additions if v in ours.get(key, []) or v not in values]
		if not wanted and key in created:
			# Leave behind no key we invented, but never take one that still holds
			# an entry the user put there.
			updated = drop_empty_array(updated, key)
	delete = not wanted and created_file and is_empty(updated)
	if delete:
		status, detail = "removed", "leos-agent created it and nothing else is in it"
	elif current == updated:
		status, detail = "unchanged", ""
	else:
		status, detail = ("updated" if existed else "created"), ""
	detail = "; ".join(part for part in (detail, note) if part)
	diff = ""
	if args.dry_run and status != "unchanged":
		diff = unified(path, current if existed else "", "" if delete else updated)
	if args.writes and status != "unchanged":
		if delete:
			remove_file(path)
		else:
			atomic_write(path, updated, crlf)
	registration.update(config=str(path), created_keys=sorted(created), created_config=created_file)
	return Result(label, status, detail, diff=diff), registration


def manage_opencode_config(root, cfg, args, receipt):
	previous = receipt.data
	recorded = previous.get("config")
	recorded = Path(recorded) if isinstance(recorded, str) and os.path.isabs(recorded) else None
	ours = {key: _strings(previous.get(key)) for key in ("instructions", "plugin")}
	created_keys = set(_strings(previous.get("created_keys")))
	created_file = previous.get("created_config") is True
	if args.uninstall:
		# The file the install edited, wherever the environment points today.
		path = recorded or opencode_config_path(cfg)
		return [edit_opencode_config(path, root, args, {}, ours, created_keys, created_file)[0]]
	path = opencode_config_path(cfg)
	results = []
	if recorded is not None and recorded != path:
		moved, _ = edit_opencode_config(recorded, root, args, {}, ours, created_keys, created_file,
			note=f"registration moved to {display(path)}")
		results.append(moved)
		ours, created_keys, created_file = {}, set(), False
	spec = opencode_cache_spec(root)
	plugin = [(root / "index.js").resolve().as_uri()]
	if spec is not None:
		existing = []
		if path.is_file():
			from jsonc_edit import properties
			existing = _strings(properties(read_text(path)[0])[1].get("plugin", (0, 0, []))[2])
		# Keep whatever spec registered the cached package; add the cache's own
		# spec only when nothing registers it.
		plugin = [] if any(v == PROVENANCE or v.startswith(PROVENANCE + "@") for v in existing) else [spec]
	wanted = {"instructions": [str(cfg / "leos-agent-routing.md")], "plugin": plugin}
	result, registration = edit_opencode_config(path, root, args, wanted, ours, created_keys, created_file)
	receipt.meta = registration
	results.append(result)
	return results


def finish_receipt(receipt, args, results):
	"""Take back copies the receipt lists that no target covers any more, then
	write (or, on uninstall, remove) the receipt."""
	out = []
	for rel, recorded in sorted(receipt.files.items()):
		path = receipt.cfg / rel
		normal = os.path.normpath(rel)
		# A receipt is a file in the user's config dir: never let it name a path outside it.
		if rel in receipt.covered or os.path.isabs(normal) or normal.split(os.sep)[0] == ".." or not path.is_file():
			continue
		if digest(path.read_bytes()) != recorded:
			out.append(Result(display(path), "preserved", "no longer shipped; edited since leos-agent wrote it"))
			continue
		if args.writes:
			remove_file(path)
		out.append(Result(display(path), "removed", "no longer shipped"))
	if not args.writes or any(r.failed for r in results + out):
		return out
	if args.uninstall:
		if receipt.path.is_file():
			remove_file(receipt.path)
	else:
		text = receipt.render()
		current = receipt.path.read_text(encoding="utf-8") if receipt.path.is_file() else None
		if current != text:
			atomic_write(receipt.path, text, False)
	return out


def _run_targets(harness, root, args):
	# Read once per run: routing.load() is used by Codex's TOML rendering and
	# Cursor's rule, both still per-machine even though the payload is not.
	config = routing.load()
	cfg = config_dir(harness)
	require_config_dir(harness, cfg)
	receipt = Receipt(cfg, harness) if harness in COPYING else None
	targets = []

	def migrate(path):
		targets.append((display(path), lambda: migrate_legacy_block(path, args, display(path))))

	def copy(dest, payload, model=None):
		receipt.covered.add(receipt.key(dest))
		targets.append((display(dest), lambda: install_file_copy(dest, payload, args, receipt, display(dest), model)))

	def retire(dest, expected=None, model=None, why="retired profile"):
		# Absent is the normal case, and says nothing worth a report line.
		if dest.is_file():
			receipt.covered.add(receipt.key(dest))
			targets.append((display(dest), lambda: retire_copy(dest, args, receipt, display(dest), expected, model, why)))

	def agents(kind):
		for name in CODEX_AGENTS:
			if kind == "toml":
				src = root / "payload" / "codex-agents" / f"{name}.toml"
				copy(cfg / "agents" / f"{name}.toml", lambda s=src: render_codex_agent(s.read_text(encoding="utf-8")))
			else:
				copy(cfg / "agents" / f"{name}.md", lambda n=name: native_agent(root, n, harness, config),
					agent_model(name, harness, config))
		for name in RETIRED_AGENTS:
			if kind == "toml":
				src = root / "payload" / "codex-agents" / f"{name}.toml"
				retire(cfg / "agents" / f"{name}.toml",
					lambda s=src: render_codex_agent(s.read_text(encoding="utf-8")) if s.is_file() else None)
			else:
				src = root / "agents" / f"{name}.md"
				retire(cfg / "agents" / f"{name}.md",
					lambda n=name, s=src: native_agent(root, n, harness, config) if s.is_file() else None,
					agent_model(name, harness, config))

	if harness in ("claude", "hermes", "pi"):
		# The plugin or extension delivers everything; only an older release's
		# policy block can be left in the harness's global instruction file.
		migrate(cfg / {"claude": "CLAUDE.md", "hermes": "SOUL.md", "pi": "AGENTS.md"}[harness])

	elif harness == "codex":
		migrate(cfg / "AGENTS.md")
		agents("toml")

	elif harness == "cursor":
		# Global native agents are supported; global ~/.cursor/rules is not, so
		# the rule an older release wrote there never loaded.
		retire(cfg / "rules" / "leos-agent-routing.mdc", why="obsolete global routing rule")
		agents("md")

	elif harness == "opencode":
		migrate(cfg / "AGENTS.md")
		copy(cfg / "leos-agent-routing.md",
			lambda: "<!-- Managed by leos-agent. -->\n" + payload_body(root, harness, config) + "\n")
		targets.append(("opencode config", lambda: manage_opencode_config(root, cfg, args, receipt)))
		agents("md")
		copy(cfg / "skills" / "leo-install" / "SKILL.md",
			lambda: opencode_payload(root / "skills" / "install" / "SKILL.md", root, rename_install=True))
		# Bound late via a default argument: a lambda closing over the loop
		# variable would copy the last entry every time.
		for skill_name in OPENCODE_SKILLS:
			for ref in sorted((root / "skills" / skill_name / "reference").glob("*.md")):
				copy(cfg / "skills" / skill_name / "reference" / ref.name, lambda s=ref: opencode_payload(s, root))
			src = root / "skills" / skill_name / "SKILL.md"
			copy(cfg / "skills" / skill_name / "SKILL.md", lambda s=src: opencode_payload(s, root))
		for command_name in OPENCODE_COMMANDS:
			dest = cfg / "commands" / f"{command_name}.md"
			targets.append((display(dest), lambda d=dest: remove_legacy_command(d, args, display(d), receipt)))

	# Each target reports on its own. One failure must not discard the report
	# for the others, or hide what already landed.
	results = []

	def attempt(label, target):
		try:
			outcome = target()
			results.extend(outcome if isinstance(outcome, list) else [outcome])
		except (BlockError, ValueError) as exc:
			results.append(Result(label, "error", str(exc)))
		except UnicodeDecodeError:
			results.append(Result(label, "error", "not valid UTF-8 text; refusing to rewrite it"))
		except OSError as exc:
			results.append(Result(label, "error", exc.strerror or str(exc)))

	for label, target in targets:
		attempt(label, target)
	if receipt is not None:
		attempt(display(receipt.path), lambda: finish_receipt(receipt, args, results))
	return results


def run(harness, root, args):
	from install_transaction import ACTIVE, Transaction, prune_empty_dirs
	try:
		cfg = config_dir(harness)
		require_config_dir(harness, cfg)
		backup = backup_path(harness)
	except ConfigError as exc:
		return [Result(harness, "error", str(exc))]
	tx = Transaction(backup, boundaries=(cfg, backup.parent))
	token = ACTIVE.set(tx) if args.writes else None
	try:
		results = _run_targets(harness, root, args)
		if not args.writes:
			return results
		if any(result.failed for result in results):
			for result in results:
				if result.changed:
					result.status = "skipped"
					result.detail = "transaction aborted; another target needs attention"
			return results
		legacy = cfg / LEGACY_BACKUP
		if tx.changes or legacy.is_file():
			prepare_backup_dir(backup)
		try:
			tx.commit()
		except OSError as exc:
			# Nothing landed and the previous backup is untouched; keep every
			# target's line so the report still says what was attempted.
			for result in results:
				if result.changed:
					result.status = "skipped"
					result.detail = "rolled back; a write failed"
			results.append(Result(harness, "error", str(exc)))
			return results
		# Remove only empty directories left by deleted owned files.
		prune_empty_dirs(tx.changes, cfg)
		migrate_legacy_backup(legacy, backup, superseded=bool(tx.changes))
		return results
	except (ValueError, OSError) as exc:
		return [Result(harness, "error", str(exc))]
	finally:
		if token is not None:
			ACTIVE.reset(token)


def migrate_legacy_backup(legacy, backup, superseded):
	"""Move a backup an older release left in the config directory to the data
	directory, or drop it once a newer backup supersedes it."""
	if not legacy.is_file():
		return
	if not superseded and not backup.exists():
		from install_transaction import replace
		replace(backup, legacy.read_bytes(), 0o600)
	legacy.unlink()


def rollback_source(harness):
	"""The backup --rollback would consume, or None."""
	from install_transaction import pending_path
	backup = backup_path(harness)
	candidates = [pending_path(backup), backup]
	try:
		candidates.append(config_dir(harness) / LEGACY_BACKUP)
	except ConfigError:
		pass
	return next((path for path in candidates if path.is_file()), None)


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
		"--uninstall", action="store_true", help="remove installed payload files and registrations this tool owns"
	)
	mode.add_argument("--check", action="store_true", help="exit 1 if anything would change")
	mode.add_argument("--rollback", action="store_true", help="undo the last install or uninstall if files have not since changed")
	parser.add_argument("--force", action="store_true", help="on install, replace a conflicting file this tool did not write")
	args = parser.parse_args(argv)
	args.writes = not (args.dry_run or args.check)
	if args.rollback:
		from install_transaction import rollback
		try:
			source = rollback_source(args.harness)
			if source is None:
				print(f"leo-install: nothing to roll back for {args.harness}; no installation backup at "
					f"{display(backup_path(args.harness))}", file=sys.stderr)
				return 1
			boundary = config_dir(args.harness)
			# Its own redo record goes beside the data-dir backups, even when the
			# source is a backup an older release left in the config dir.
			redo = backup_path(args.harness).with_name(f"{args.harness}-redo.json")
			prepare_backup_dir(redo)
			print(f"restored {rollback(source, boundary, redo=redo)} files")
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
	preserved = [r for r in results if r.status == "preserved"]

	if failed:
		print(f"\n{len(failed)} target(s) could not be handled; nothing was written")
		return 1
	if preserved:
		print(f"\n{len(preserved)} file(s) preserved as they are; see above")
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
