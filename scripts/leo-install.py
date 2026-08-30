#!/usr/bin/env python3
"""Install leos-agent preferences into one harness's global instruction file.

Injects the payload from rules/preferences.md into the calling harness's global
instruction file, wrapped in a <leos-agent> block, and installs any files that
harness needs but cannot receive through its plugin system.

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

# The routing region inside the payload, replaced per harness at install time.
ROUTING_OPEN = "<!-- leos-agent:routing -->"
ROUTING_CLOSE = "<!-- /leos-agent:routing -->"

OPEN_RE = re.compile(r"^<leos-agent\b[^>]*>[ \t]*$", re.MULTILINE)
CLOSE_RE = re.compile(r"^</leos-agent>[ \t]*$", re.MULTILINE)

# Codex concatenates the whole AGENTS.md chain under a byte cap; warn before a
# global file large enough to start crowding out repo-level instructions.
CODEX_SOFT_CAP = 28 * 1024

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
OPENCODE_SKILLS = ("doctor", "review-pr", "handoff", "handon", "tune-routing")
OPENCODE_COMMANDS = ("review-pr", "handoff", "handon")

# Codex plugins cannot package custom agent definitions directly, so these are
# copied into ~/.codex/agents. Keep this tuple authoritative: check.py and the
# installer tests derive the expected payload from it.
CODEX_AGENTS = ("leo-runner", "leo-executor")


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
		return self.status in ("created", "updated", "removed")

	@property
	def failed(self):
		return self.status in ("error", "conflict")

	def line(self, pending):
		status = self.status
		if pending and self.changed:
			status = {"created": "to create", "updated": "to update", "removed": "to remove"}[status]
		suffix = f" ({self.detail})" if self.detail else ""
		return f"  {status:9} {self.target}{suffix}"


def plugin_root():
	for name in ("LEOS_AGENT_ROOT", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"):
		value = os.environ.get(name)
		if not value:
			continue
		root = Path(value).expanduser()
		if (root / "rules" / "preferences.md").is_file():
			return root.resolve()
	return Path(__file__).resolve().parent.parent


def read_version(root):
	manifest = root / "package.json"
	try:
		return json.loads(manifest.read_text(encoding="utf-8"))["version"]
	except FileNotFoundError:
		sys.exit(f"leo-install: {manifest} is missing; the plugin install looks incomplete")
	except json.JSONDecodeError as exc:
		sys.exit(f"leo-install: {manifest} is not valid JSON ({exc})")
	except KeyError:
		sys.exit(f"leo-install: {manifest} has no version field")


def render_routing(body, harness, config):
	"""Replace the routing region with the stanza for this machine's config.

	The payload ships with a default inside the region, so an un-rendered read of
	rules/preferences.md -- Cursor's plugin-delivered rule, a human opening the
	file -- still says something true. Rendering only ever narrows it to the one
	harness being installed, which is why the installed payload is smaller than
	the file on disk rather than larger.
	"""
	start = body.find(ROUTING_OPEN)
	end = body.find(ROUTING_CLOSE)
	if start < 0 or end < start:
		sys.exit(f"leo-install: rules/preferences.md is missing its {ROUTING_OPEN} region")
	return body[:start] + routing.stanza(harness, config) + body[end + len(ROUTING_CLOSE):]


def payload_body(root, harness=None, config=None):
	"""The canonical payload: rules/preferences.md with its frontmatter stripped.

	With a harness, the routing region is rendered for it; without one the region
	keeps its shipped default, markers and all.
	"""
	text = (root / "rules" / "preferences.md").read_text(encoding="utf-8")
	body = re.sub(r"(?s)\A---\n.*?\n---\n", "", text, count=1).strip()
	if not body:
		sys.exit("leo-install: rules/preferences.md has no body below its frontmatter")
	if OPEN_RE.search(body) or CLOSE_RE.search(body):
		sys.exit("leo-install: rules/preferences.md contains a <leos-agent> marker; it must not")
	if harness:
		body = render_routing(body, harness, config if config is not None else routing.load())
	return body


def build_block(root, harness=None, config=None):
	return f'<leos-agent version="{read_version(root)}">\n{payload_body(root, harness, config)}\n</leos-agent>\n'


def render_codex_agent(text, agent_name, config):
	"""Substitute a Codex profile's model, leaving the shipped default when unset."""
	entry = routing.profile(config, "codex", agent_name.split("-", 1)[1])
	if not entry:
		return text
	text = re.sub(r'(?m)^model = ".*"$', f'model = "{entry["model"]}"', text, count=1)
	if entry["effort"]:
		text = re.sub(
			r'(?m)^model_reasoning_effort = ".*"$',
			f'model_reasoning_effort = "{entry["effort"]}"',
			text,
			count=1,
		)
	return text


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
			path.unlink()
		return Result(label, "removed", "file held nothing else, deleted")

	return write_if_changed(path, inject(current, block), current, existed, crlf, args, label)


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
	foreign = existed and current != payload and PROVENANCE not in current
	if foreign and not args.force:
		return Result(label, "conflict", "a file we did not write is already here; re-run with --force to replace it")

	if args.uninstall:
		if not existed:
			return Result(label, "unchanged", "not present")
		if args.writes:
			dest.unlink()
			if owned_parent and dest.parent.is_dir() and not any(dest.parent.iterdir()):
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
	return text


def run(harness, root, args):
	# Read once per run: rendering has to be a pure function of (version, config)
	# or a second install would not come back "unchanged".
	config = routing.load()
	block = build_block(root, harness, config)
	home = Path.home()
	targets = []

	if harness == "claude":
		label = "~/.claude/CLAUDE.md"
		targets.append((label, lambda: install_markdown(home / ".claude" / "CLAUDE.md", block, args, label)))

	elif harness == "codex":
		targets.append(("~/.codex/AGENTS.md", lambda: install_codex_agents_md(home, block, args)))
		for agent_name in CODEX_AGENTS:
			label = f"~/.codex/agents/{agent_name}.toml"
			targets.append(
				(
					label,
					lambda n=agent_name, l=label: install_file_copy(
						root / "payload" / "codex-agents" / f"{n}.toml",
						home / ".codex" / "agents" / f"{n}.toml",
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
		# The payload itself arrives natively through the plugin's alwaysApply
		# rule, which is the file in the plugin directory -- so there is nothing
		# per-machine in it. Only the routing stanza needs installing, and only
		# as its own rule -- and only when something is actually configured: an
		# unconfigured rule would restate the payload's default, an always-loaded
		# no-op that costs context on every turn.
		label = "~/.cursor/rules/leos-agent-routing.mdc"
		dest = home / ".cursor" / "rules" / "leos-agent-routing.mdc"
		configured = bool(
			routing.profile(config, "cursor", "runner") or routing.profile(config, "cursor", "executor")
		)

		def cursor_rule_target(l=label):
			if args.uninstall or configured:
				return install_file_copy(None, dest, args, l, payload=cursor_routing_rule(harness, config))
			# Unconfigured install: write nothing, and take back a stale rule a
			# previous config left behind -- but only one that is provably ours.
			if not dest.is_file():
				return Result(l, "skipped", "no routing configured for cursor")
			if PROVENANCE not in dest.read_text(encoding="utf-8"):
				return Result(l, "skipped", "no routing configured; leaving the unrelated file at this path")
			if args.writes:
				dest.unlink()
			return Result(l, "removed", "no routing configured; stale rule removed")

		targets.append((label, cursor_rule_target))

	elif harness == "hermes":
		# Never create SOUL.md: Hermes writes its own starter identity file on
		# first run, and pre-empting that would fight the bootstrap.
		label = "~/.hermes/SOUL.md"
		targets.append(
			(label, lambda: install_markdown(home / ".hermes" / "SOUL.md", block, args, label, create=False))
		)

	elif harness == "pi":
		label = "~/.pi/agent/AGENTS.md"
		targets.append(
			(label, lambda: install_markdown(home / ".pi" / "agent" / "AGENTS.md", block, args, label))
		)

	elif harness == "opencode":
		cfg = home / ".config" / "opencode"
		label = "~/.config/opencode/AGENTS.md"
		targets.append((label, lambda: install_markdown(cfg / "AGENTS.md", block, args, label)))
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
			command_label = f"~/.config/opencode/commands/{command_name}.md"
			targets.append(
				(
					command_label,
					lambda n=command_name, l=command_label: install_file_copy(
						root / "commands" / f"{n}.md",
						cfg / "commands" / f"{n}.md",
						args,
						l,
						payload=opencode_payload(root / "commands" / f"{n}.md", root),
					),
				)
			)

	# Each target reports on its own. One failure must not discard the report
	# for the others, or hide what already landed.
	results = []
	for label, target in targets:
		try:
			results.append(target())
		except BlockError as exc:
			results.append(Result(label, "error", str(exc)))
		except UnicodeDecodeError:
			results.append(Result(label, "error", "not valid UTF-8 text; refusing to rewrite it"))
		except OSError as exc:
			results.append(Result(label, "error", exc.strerror or str(exc)))
	return results


def install_codex_agents_md(home, block, args):
	"""Codex's AGENTS.md, with a warning when the result nears the chain cap."""
	path = home / ".codex" / "AGENTS.md"
	result = install_markdown(path, block, args, "~/.codex/AGENTS.md")
	if args.uninstall or result.status in ("skipped", "error"):
		return result
	current = read_text(path)[0] if path.is_file() else ""
	prospective = current if result.status == "unchanged" else inject(current, block)
	size = len(prospective.encode("utf-8"))
	if size > CODEX_SOFT_CAP:
		result.detail = f"WARNING: {size} bytes, near Codex's 32KiB chain cap"
	return result


def main(argv=None):
	parser = argparse.ArgumentParser(
		prog="leo-install.py",
		description="Install leos-agent preferences into ONE harness's global instruction file.",
	)
	parser.add_argument("harness", choices=HARNESSES, help="the harness this session is running in")
	mode = parser.add_mutually_exclusive_group()
	mode.add_argument("--dry-run", action="store_true", help="show diffs, write nothing")
	mode.add_argument("--uninstall", action="store_true", help="remove the block and any installed payload files")
	mode.add_argument("--check", action="store_true", help="exit 1 if anything would change")
	parser.add_argument("--force", action="store_true", help="replace a conflicting file this tool did not write")
	args = parser.parse_args(argv)
	args.writes = not (args.dry_run or args.check)

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
	return 0


if __name__ == "__main__":
	sys.exit(main())
