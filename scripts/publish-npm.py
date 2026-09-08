#!/usr/bin/env python3
"""Publish leos-agent to npm exactly once per version.

Two properties matter here, and both are inherited from the release path this
replaces. Publishing is idempotent: an exact version already on the registry is
a no-op, so re-running a tag is safe, while a lookup that fails for any reason
other than a confirmed 404 aborts rather than guessing. And the tree npm would
actually ship is inspected before it ships, because `files` in package.json
scopes the publish but does not exclude build residue that lands inside a
directory it lists.

Once npm accepts the upload the release has happened, so nothing after that
point fails this script. The registry's read path can lag a publish by minutes;
reporting that lag as a failure marked three consecutive successful releases
red, and a release signal nobody trusts is worse than none.

Authentication is npm's OIDC trusted publishing: the workflow's `id-token`
permission supplies a short-lived credential, so there is no token to read here.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "leos-agent"

# Residue that a local checkout accumulates and a publish must never carry.
FORBIDDEN_PARTS = ("__pycache__",)
FORBIDDEN_SUFFIXES = (".pyc", ".log")
FORBIDDEN_NAMES = (".DS_Store",)
REQUIRED_FILES = {
	"LICENSE", "package.json", "index.js", "pi-extension.js", "rules/preferences.md",
	"payload/model-prices.json", "payload/legacy-copy-hashes.json",
	"scripts/leo-install.py", "scripts/install_transaction.py", "scripts/jsonc_edit.py",
	"scripts/routing.py", "scripts/routing_engine.py", "scripts/pricing.py",
	"scripts/harness_bridge.js", "scripts/dispatch_guard.py", "scripts/dispatch_log.py",
	"scripts/session_models.py", "scripts/payload.py", "scripts/state.py",
	"scripts/doctor.py", "skills/install/SKILL.md",
} | {f"agents/leo-{name}.md" for name in ("cheap", "standard", "parent", "reviewer", "runner", "executor")}


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
	missing = REQUIRED_FILES - set(inventory)
	if missing:
		raise ReleaseError("publish tree is missing required runtime files: " + ", ".join(sorted(missing)))
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


# Roughly two minutes. The registry's read path lagged a real publish past the
# previous 35-second budget on three consecutive releases, so every one of them
# reported failure while actually succeeding.
PROPAGATION_DELAYS = (0, 5, 10, 20, 30, 60)


def wait_for_public_version(version, npm="npm", delays=PROPAGATION_DELAYS):
	"""Allow registry propagation; never retry the upload or bypass staging.

	A lookup that fails for a reason other than a confirmed 404 still raises: an
	outage is not propagation, and the caller decides what to do about it.
	"""
	for delay in delays:
		if delay:
			time.sleep(delay)
		if registry_state(version, npm) == "present":
			return True
	return False


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
		print(f"{PACKAGE} {version}: {len(inventory)} file(s) staged for publish", flush=True)

		state = registry_state(version, args.npm)
		if state == "present":
			print(f"{PACKAGE}@{version} is already on the registry; nothing to do", flush=True)
			return 0
		if args.dry_run:
			print(f"would publish {PACKAGE}@{version}", flush=True)
			return 0

		publish(args.npm)
		# The upload is accepted and cannot be taken back, so nothing below may fail
		# the release. Everything here only reports whether the registry is serving
		# the version yet, and treating "not yet" as a failure marked three
		# consecutive successful releases red -- which is how a release signal stops
		# being read at all. A publish that genuinely failed raises inside publish().
		try:
			confirmed, unverified = wait_for_public_version(version, args.npm), None
		except ReleaseError as exc:
			confirmed, unverified = False, f"the registry lookup failed: {exc}"
		if confirmed:
			print(f"published and verified {PACKAGE}@{version}", flush=True)
			return 0
		print(f"published {PACKAGE}@{version}", flush=True)
		print(
			f"warning: {unverified or 'the registry is not serving it yet'}. "
			f"npm accepted the upload; confirm with `npm view {PACKAGE}@{version} version`. "
			"Re-running this script is safe: it stops at the pre-publish check once the "
			"version is visible.",
			file=sys.stderr,
		)
		return 0
	except ReleaseError as exc:
		print(f"error: {exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
