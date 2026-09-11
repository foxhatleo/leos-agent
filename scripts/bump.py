#!/usr/bin/env python3
"""Prepare a release using 12.YYYYMMDDXX.0 (UTC, serial 00 through 99).

Run explicitly for a release, never from a commit hook. Validate every rewrite
before replacing files, and restore previous contents if a replacement fails.
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
	match = re.fullmatch(r"(\d+)\.(\d+)\.\d+", current)
	if match is None:
		raise BumpError(f"package.json version {current!r} is not major.minor.patch")
	major, minor = match.groups()
	date = today().strftime("%Y%m%d")
	serial = 0
	if major == "12" and len(minor) == 10:
		if minor[:8] > date:
			raise BumpError("refusing a release older than the current version's UTC date")
		if minor[:8] == date:
			serial = int(minor[8:]) + 1
	if serial > 99:
		raise BumpError(f"{date} already has 100 releases; wait for the next UTC day")
	if int(major) > 12:
		raise BumpError("refusing to downgrade a future major version")
	return f"12.{date}{serial:02d}.0"


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
	# A version-free README needs no release rewrite.
	return text.replace(old, new)


# Release-owned files. Commit hooks never stage these files.
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
		os.fchmod(fd, path.stat().st_mode & 0o777)
		with os.fdopen(fd, "wb") as handle:
			handle.write(data)
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(tmp_path, path)
	except BaseException:
		with contextlib.suppress(FileNotFoundError):
			os.unlink(tmp_path)
		raise


def rewrite_all(root, old, new, dry_run=False):
	"""Validate all rewrites first, then replace with rollback on failure."""
	prepared = []
	for rel, rewrite in REWRITE.items():
		path = root / rel
		original = path.read_text(encoding="utf-8")
		updated = rewrite(original, old, new, rel)
		if updated != original:
			prepared.append((rel, original, updated))
	applied = []
	try:
		if not dry_run:
			for rel, original, updated in prepared:
				write_atomic(root / rel, updated)
				applied.append((rel, original))
	except BaseException:
		for rel, original in reversed(applied):
			write_atomic(root / rel, original)
		raise
	return [rel for rel, _, _ in prepared]


def do_check(root):
	old = current_version(root)
	date = today().strftime("%Y%m%d")
	match = re.fullmatch(r"12\.(\d{8})\d{2}\.0", old)
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
