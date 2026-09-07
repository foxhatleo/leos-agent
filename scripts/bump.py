#!/usr/bin/env python3
"""Compute and apply the next leos-agent version across every manifest.

Scheme: `11.YYYYMMDDX.0`. Major is pinned at 11; minor is the UTC calendar date
with a single trailing digit for the Nth release that day; patch is always 0.
The trailing digit exists so several releases in one day still sort correctly,
and it MUST stay one digit -- a tenth release would produce `...07010`, which
sorts above the next day's `...080`, silently inverting version order. That is
a hard error here rather than a version nobody notices is wrong until a stale
build outranks a fresh one.

This is deliberately a thin layer over string replacement, not a JSON
round-trip: the manifests are hand-edited and hand-read, and a `json.dump`
would reflow key order and quoting that nobody asked to change.
"""

import argparse
import contextlib
import datetime
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class BumpError(Exception):
	"""The version cannot be computed or applied safely; stop, do not guess."""


def today():
	"""UTC calendar date the bump is computed against.

	Its own function so a test can pin a date instead of racing the real clock
	(patch this with unittest.mock rather than threading a date parameter
	through every call below).
	"""
	return datetime.datetime.now(datetime.timezone.utc).date()


def current_version(root):
	text = (root / "package.json").read_text(encoding="utf-8")
	match = re.search(r'"version":\s*"([^"]+)"', text)
	if match is None:
		raise BumpError("package.json: no \"version\" field found")
	return match.group(1)


def next_version(current):
	match = re.fullmatch(r"\d+\.(\d+)\.\d+", current)
	if match is None:
		raise BumpError(f"package.json version {current!r} is not major.minor.patch")
	minor = match.group(1)
	date = today().strftime("%Y%m%d")
	# Same-day re-run: keep the date, bump the trailing serial. Anything else
	# (a stale date, or the pre-date-scheme 10.x line) starts the day at 0.
	if minor.startswith(date) and len(minor) == len(date) + 1:
		serial = int(minor[len(date):]) + 1
	else:
		serial = 0
	if serial > 9:
		raise BumpError(
			f"{date} already has release 9 (minor {minor}); a 10th release would produce minor "
			f"{date}10, which sorts above the next day's version -- this is the ordering "
			"inversion bump.py exists to refuse. Wait for UTC midnight, or pick a different scheme."
		)
	return f"11.{date}{serial}.0"


def rewrite_json_version(text, old, new, label):
	# Anchored on the quoted key so a version-looking substring elsewhere in the
	# file (there is none today, but nothing guarantees that) can't match instead.
	pattern = re.compile(r'("version":\s*")' + re.escape(old) + r'(")')
	updated, count = pattern.subn(lambda m: m.group(1) + new + m.group(2), text, count=1)
	if count != 1:
		raise BumpError(f'{label}: could not find "version": "{old}"')
	return updated


def rewrite_yaml_version(text, old, new, label):
	pattern = re.compile(r"(?m)^(version:\s*)" + re.escape(old) + r"\s*$")
	updated, count = pattern.subn(lambda m: m.group(1) + new, text, count=1)
	if count != 1:
		raise BumpError(f"{label}: could not find version: {old}")
	return updated


def rewrite_readme(text, old, new, label):
	# check.py fails the build on ANY stray semver-shaped string in README, so
	# every occurrence -- prose, install paths, the cachebuster example -- has
	# to move, not just the first.
	if old not in text:
		raise BumpError(f"{label}: does not mention version {old}")
	return text.replace(old, new)


# Every file bump.py owns, in the order it touches them. .githooks/pre-commit
# stages exactly this set after running this script.
REWRITE = {
	"package.json": rewrite_json_version,
	".claude-plugin/plugin.json": rewrite_json_version,
	".codex-plugin/plugin.json": rewrite_json_version,
	".cursor-plugin/plugin.json": rewrite_json_version,
	".claude-plugin/marketplace.json": rewrite_json_version,
	"plugin.yaml": rewrite_yaml_version,
	"README.md": rewrite_readme,
}


def write_atomic(path, text):
	data = text.encode("utf-8")
	fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
	try:
		with os.fdopen(fd, "wb") as handle:
			handle.write(data)
		os.replace(tmp_path, path)
	except BaseException:
		with contextlib.suppress(FileNotFoundError):
			os.unlink(tmp_path)
		raise


def rewrite_all(root, old, new, dry_run=False):
	"""Apply every REWRITE entry; return the paths (relative to root) that changed.

	Skips a file whose rewritten bytes are identical to what's on disk, so a
	re-run after a partial failure doesn't touch files it already fixed.
	"""
	changed = []
	for rel, rewrite in REWRITE.items():
		path = root / rel
		original = path.read_text(encoding="utf-8")
		updated = rewrite(original, old, new, rel)
		if updated == original:
			continue
		changed.append(rel)
		if not dry_run:
			write_atomic(path, updated)
	return changed


def do_check(root):
	old = current_version(root)
	date = today().strftime("%Y%m%d")
	match = re.fullmatch(r"11\.(\d{8})\d\.0", old)
	if match and match.group(1) == date:
		print(f"{old} is today's version")
		return 0
	new = next_version(old)
	print(f"{old} is not today's version; next would be {new}", file=sys.stderr)
	return 1


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--check", action="store_true", help="exit non-zero if the version is not today's")
	parser.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
	parser.add_argument("--print", action="store_true", dest="print_only", help="print the chosen version and exit")
	args = parser.parse_args(argv)

	try:
		if args.check:
			return do_check(ROOT)

		old = current_version(ROOT)
		new = next_version(old)

		if args.print_only:
			print(new)
			return 0

		changed = rewrite_all(ROOT, old, new, dry_run=args.dry_run)
		verb = "would bump" if args.dry_run else "bumped"
		print(f"{verb} {old} -> {new}")
		for rel in changed:
			print(f"  {rel}")
		return 0
	except BumpError as exc:
		print(f"error: {exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
