#!/usr/bin/env python3
"""Publish leos-agent to npm exactly once per version.

Two properties matter here, and both are inherited from the release path this
replaces. Publishing is idempotent: an exact version already on the registry is
a no-op, so re-running a tag is safe, while a lookup that fails for any reason
other than a confirmed 404 aborts rather than guessing. And the tree npm would
actually ship is inspected before it ships, because `files` in package.json
scopes the publish but does not exclude build residue that lands inside a
directory it lists.

Authentication is npm's OIDC trusted publishing: the workflow's `id-token`
permission supplies a short-lived credential, so there is no token to read here.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "leos-agent"

# Residue that a local checkout accumulates and a publish must never carry.
FORBIDDEN_PARTS = ("__pycache__",)
FORBIDDEN_SUFFIXES = (".pyc", ".log")
FORBIDDEN_NAMES = (".DS_Store",)


class ReleaseError(Exception):
	"""The release cannot proceed safely; the caller should stop, not retry."""


def run(command):
	return subprocess.run(command, capture_output=True, text=True, check=False, cwd=ROOT)


def declared_version():
	version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
	return version


def pack_inventory(npm="npm"):
	"""Return the file list npm would publish, without publishing it."""
	packed = run([npm, "pack", "--dry-run", "--json"])
	if packed.returncode:
		raise ReleaseError(f"npm pack --dry-run failed: {(packed.stdout + packed.stderr).strip()}")
	try:
		report = json.loads(packed.stdout)
	except json.JSONDecodeError as exc:
		raise ReleaseError(f"npm pack --dry-run emitted unparseable JSON: {exc}") from exc
	if not report:
		raise ReleaseError("npm pack --dry-run reported no package")
	return sorted(entry["path"] for entry in report[0].get("files", []))


def forbidden_paths(inventory):
	found = []
	for path in inventory:
		parts = path.split("/")
		if any(part in FORBIDDEN_PARTS for part in parts):
			found.append(path)
		elif path.endswith(FORBIDDEN_SUFFIXES) or parts[-1] in FORBIDDEN_NAMES:
			found.append(path)
	return found


def check_inventory(inventory):
	if "LICENSE" not in inventory:
		raise ReleaseError("publish tree has no LICENSE")
	if "package.json" not in inventory:
		raise ReleaseError("publish tree has no package.json")
	found = forbidden_paths(inventory)
	if found:
		raise ReleaseError("publish tree contains transient files: " + ", ".join(found))


def registry_state(version, npm="npm"):
	"""Report whether this exact version is already on the registry.

	Anything other than a clean hit or a confirmed not-found is an error: an
	auth failure or a registry outage must not be read as "absent, publish it".
	"""
	viewed = run([npm, "view", f"{PACKAGE}@{version}", "version"])
	output = (viewed.stdout + viewed.stderr).strip()
	if viewed.returncode == 0:
		if output != version:
			raise ReleaseError(f"npm returned {output!r}, not the exact version {version!r}")
		return "present"
	if "E404" in output or "404 Not Found" in output:
		return "absent"
	raise ReleaseError(f"npm version lookup failed without a confirmed not-found: {output}")


def publish(npm="npm"):
	published = run([npm, "publish", "--access", "public"])
	if published.returncode:
		raise ReleaseError(f"npm publish failed: {(published.stdout + published.stderr).strip()}")
	return (published.stdout + published.stderr).strip()


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--tag", help="git tag being released; must match package.json")
	parser.add_argument("--dry-run", action="store_true", help="check everything, publish nothing")
	parser.add_argument("--npm", default="npm", help="npm executable to use")
	args = parser.parse_args(argv)

	try:
		version = declared_version()
		if args.tag is not None:
			expected = args.tag[1:] if args.tag.startswith("v") else args.tag
			if expected != version:
				raise ReleaseError(f"tag {args.tag!r} does not match package.json version {version!r}")

		inventory = pack_inventory(args.npm)
		check_inventory(inventory)
		print(f"{PACKAGE} {version}: {len(inventory)} file(s) staged for publish")

		state = registry_state(version, args.npm)
		if state == "present":
			print(f"{PACKAGE}@{version} is already on the registry; nothing to do")
			return 0
		if args.dry_run:
			print(f"would publish {PACKAGE}@{version}")
			return 0

		publish(args.npm)
		print(f"published {PACKAGE}@{version}")
		return 0
	except ReleaseError as exc:
		print(f"error: {exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
