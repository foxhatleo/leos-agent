#!/usr/bin/env python3
"""payload: turn rules/preferences.md into the text a harness actually loads.

Extracted from leo-install.py so that the session-start emitter and the
installer render from ONE implementation. Two renderers would be two chances to
drift, and a payload that differs by which code path produced it is exactly the
bug this module exists to make impossible.

Rendering must be a pure function of (version, routing config). Nothing here may
read the clock, the working directory, or git state: the result is a cached
prompt prefix, and a byte that varies between two runs costs a full cache write
on every session rather than a tenth of one.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routing  # noqa: E402  owns the harness list and the machine-local model config

# The routing region inside the payload, replaced per harness at render time.
ROUTING_OPEN = "<!-- leos-agent:routing -->"
ROUTING_CLOSE = "<!-- /leos-agent:routing -->"

# The opener is the bare tag or the tag with attributes. `\b` alone would also
# accept <leos-agent-notes> or <leos-agent:x>, read a user's own tag as a
# marker, and abort the install over a pairing error that is not there.
OPEN_RE = re.compile(r"^<leos-agent(?:[ \t][^>\n]*)?>[ \t]*$", re.MULTILINE)
CLOSE_RE = re.compile(r"^</leos-agent>[ \t]*$", re.MULTILINE)


# Policy text Claude Code's own Agent tool guidance already states (do small work
# yourself, brief with goal and file pointers instead of retyping, never repeat
# delegated work, parallelise only independent pieces). Dropped from the Claude
# render only, to save always-loaded bytes; every other harness keeps the full
# policy. Entries match rules/preferences.md ignoring line wrapping, and a
# leading separator belongs to the entry so the prose stays well formed.
# scripts/check.py fails if an entry stops matching the policy.
CLAUDE_DROP = (
	"Do not repeat finished investigation to hand it off.",
	"Pass large briefs and diffs as file paths, not inline.",
	"Do not copy history or repeat returned work.",
	"; parallel work must justify its integration cost",
)


class PayloadError(ValueError):
	pass


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
		raise PayloadError(f"leos-agent: {manifest} is missing; the plugin install looks incomplete")
	except json.JSONDecodeError as exc:
		raise PayloadError(f"leos-agent: {manifest} is not valid JSON ({exc})")
	except KeyError:
		raise PayloadError(f"leos-agent: {manifest} has no version field")


def render_routing(body, harness, config):
	"""Replace the routing region with the stanza for this machine's config.

	The payload ships with a default inside the region, so an un-rendered read of
	rules/preferences.md -- Cursor's plugin-delivered rule, a human opening the
	file -- still says something true. Rendering only ever narrows it to the one
	harness being loaded, which is why the rendered payload is smaller than the
	file on disk rather than larger.
	"""
	start = body.find(ROUTING_OPEN)
	end = body.find(ROUTING_CLOSE)
	if start < 0 or end < start:
		raise PayloadError(f"leos-agent: rules/preferences.md is missing its {ROUTING_OPEN} region")
	return body[:start] + routing.stanza(harness, config) + body[end + len(ROUTING_CLOSE):]


def _drop_pattern(entry):
	return re.compile(r"\s*" + r"\s+".join(re.escape(word) for word in entry.split()))


def drop_claude_redundant(body):
	"""Remove CLAUDE_DROP entries from a body; a missing entry is an error."""
	for entry in CLAUDE_DROP:
		body, count = _drop_pattern(entry).subn("", body, count=1)
		if not count:
			raise PayloadError(f"leos-agent: CLAUDE_DROP entry no longer matches the policy: {entry!r}")
	return body


def payload_body(root, harness=None, config=None):
	"""The canonical payload: rules/preferences.md with its frontmatter stripped.

	With a harness, the routing region is rendered for it; without one the region
	keeps its shipped default, markers and all.
	"""
	text = (root / "rules" / "preferences.md").read_text(encoding="utf-8")
	body = re.sub(r"(?s)\A---\n.*?\n---\n", "", text, count=1).strip()
	if not body:
		raise PayloadError("leos-agent: rules/preferences.md has no body below its frontmatter")
	if OPEN_RE.search(body) or CLOSE_RE.search(body):
		raise PayloadError("leos-agent: rules/preferences.md contains a <leos-agent> marker; it must not")
	if harness:
		body = render_routing(body, harness, config if config is not None else routing.load())
		if harness == "claude":
			body = drop_claude_redundant(body)
	return body


def build_block(root, harness=None, config=None):
	"""The payload wrapped in its marker block.

	Only migration and uninstall still need this: no harness has the block
	written into a global instruction file any more. It stays because
	leo-install.py must recognise and remove the blocks earlier versions wrote.
	"""
	return f'<leos-agent version="{read_version(root)}">\n{payload_body(root, harness, config)}\n</leos-agent>\n'
