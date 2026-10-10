#!/usr/bin/env python3
"""Publish leos-agent to npm exactly once per version.

Two properties matter here, and both are inherited from the release path this
replaces. Publishing is idempotent: an exact version already on the registry is
a no-op, so re-running a tag is safe, while a lookup that fails for any reason
other than a confirmed 404 aborts rather than guessing. And the tree npm would
actually ship is inspected before it ships, because `files` in package.json
scopes the publish but does not exclude build residue that lands inside a
directory it lists, and nothing else notices a runtime file it leaves out.

`latest` only ever moves forward. A version older than the registry's newest
release -- an old tag re-run after its publish failed -- goes out under its own
dist-tag instead, so installs keep resolving the newest release.

Once npm accepts the upload the release has happened, so nothing after that
point fails this script. The registry's read path can lag a publish by minutes;
reporting that lag as a failure marked three consecutive successful releases
red, and a release signal nobody trusts is worse than none.

Authentication is npm's OIDC trusted publishing: the workflow's `id-token`
permission supplies a short-lived credential, so there is no token to read here.
"""

import argparse
import json
import posixpath
import re
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

# The dist-tag a version goes out under when the registry already holds a newer
# release. A plain `npm publish` applies `latest`, so re-running an older tag
# whose publish failed would move `latest` backwards on npm 10; npm 11 instead
# refuses to apply `latest` implicitly, which leaves that tag unpublishable.
BACKFILL_TAG = "backfill"

# What the runtime reads through path joins, which no reference scan can see.
# Everything else an install needs is derived by required_files().
DATA_FILES = (
	"LICENSE", "package.json", "rules/preferences.md",
	"payload/model-prices.json", "payload/legacy-copy-hashes.json",
)

# Hermes loads __init__.py from a checkout rather than from npm, so it is not in
# `files`. It runs the same shared scripts, though, so it seeds the scan; and
# one closure for every adapter is simpler to trust than one per harness.
SCAN_ONLY = ("__init__.py",)

# How one runtime file names another. A path literal under a shipped directory;
# a skill's own `reference/` file, named relative to the skill; a bare script
# name, which harness_bridge.js and __init__.py both resolve under scripts/; a
# Python import of a sibling module; a relative JavaScript import.
PATH_REFERENCE = re.compile(r"(?<![\w.-])((?:agents|hooks|payload|rules|scripts|skills|skills-claude)/[\w./-]*\w)")
SKILL_REFERENCE = re.compile(r"(?<![\w./-])(reference/[\w./-]*\w)")
SCRIPT_NAME = re.compile(r"""['"]([\w-]+\.py)['"]""")
PY_IMPORT = re.compile(r"(?m)^[ \t]*(?:from[ \t]+(\w+)[ \t]+import|import[ \t]+(\w+(?:[ \t]*,[ \t]*\w+)*))")
JS_IMPORT = re.compile(r"""\b(?:from|import)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]""")


class ReleaseError(Exception):
	"""The release cannot proceed safely; the caller should stop, not retry."""


def run(command):
	return subprocess.run(command, capture_output=True, text=True, check=False, cwd=ROOT)


def declared_version():
	version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
	return version


def release_key(version):
	"""Order release versions numerically; None for anything not plain major.minor.patch."""
	match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version) if isinstance(version, str) else None
	return tuple(int(part) for part in match.groups()) if match else None


def references(root, rel):
	"""The shipped or scanned files `rel` names, as paths relative to the root."""
	path = root / rel
	text = path.read_text(encoding="utf-8")
	found = set(PATH_REFERENCE.findall(text))
	parts = rel.split("/")
	if path.suffix == ".md" and len(parts) > 2 and parts[0] in ("skills", "skills-claude"):
		found.update(f"{parts[0]}/{parts[1]}/{ref}" for ref in SKILL_REFERENCE.findall(text))
	if path.suffix in (".py", ".js"):
		found.update(f"scripts/{name}" for name in SCRIPT_NAME.findall(text))
	if path.suffix == ".py":
		for single, listed in PY_IMPORT.findall(text):
			for name in (single or listed).split(","):
				found.add(f"scripts/{name.strip()}.py")
	if path.suffix == ".js":
		base = posixpath.dirname(rel)
		found.update(posixpath.normpath(posixpath.join(base, spec)) for spec in JS_IMPORT.findall(text))
	return {ref for ref in found if (root / ref).is_file()}


def entry_points(root=ROOT):
	"""Files a harness loads from an npm install directly, before any of them runs."""
	manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
	pi = manifest.get("pi", {})
	entries = {manifest["main"], *pi.get("extensions", [])}
	for directory in pi.get("skills", []):
		entries.update(p.relative_to(root).as_posix() for p in sorted((root / directory).glob("*/SKILL.md")))
	entries.update(p.relative_to(root).as_posix() for p in sorted((root / "hooks").glob("*.json")))
	return {posixpath.normpath(entry) for entry in entries}


def required_files(root=ROOT):
	"""Every file an npm install needs at runtime, derived from what loads it.

	Starts from the package's entry points (package.json's main and Pi metadata,
	the hook manifests) and follows each reference until nothing new turns up,
	so a script an adapter starts running cannot be left out of this list by
	someone forgetting to add it here.
	"""
	required = set(DATA_FILES)
	required.update(p.relative_to(root).as_posix() for p in sorted((root / "agents").glob("*.md")))
	pending = sorted(entry_points(root)) + [rel for rel in SCAN_ONLY if (root / rel).is_file()]
	seen = set()
	while pending:
		rel = pending.pop()
		if rel in seen:
			continue
		seen.add(rel)
		# Only text that can name another file is read; anything else it names
		# (an image, an archive) is required but not opened.
		if posixpath.splitext(rel)[1] in (".py", ".js", ".json", ".md"):
			pending.extend(sorted(references(root, rel) - seen))
	required.update(seen - set(SCAN_ONLY))
	return frozenset(required)


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


def check_inventory(inventory, required=None):
	required = required_files() if required is None else required
	missing = set(required) - set(inventory)
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


def newest_release(npm="npm"):
	"""The newest release the registry holds, or None before the first publish.

	Both `latest` and the full version list count. `latest` is what installs
	resolve, but it can be moved by hand, and npm 11's own guard compares
	against the highest version published; a publish must not take `latest`
	when either is newer. Prerelease versions are skipped, as npm skips them.
	Like registry_state, anything but a clean answer or a confirmed 404 refuses.
	"""
	viewed = run([npm, "view", PACKAGE, "dist-tags", "versions", "--json"])
	if viewed.returncode:
		output = (viewed.stdout + viewed.stderr).strip()
		if "E404" in output or "404 Not Found" in output:
			return None
		raise ReleaseError(f"npm dist-tag lookup failed without a confirmed not-found: {output}")
	try:
		report = json.loads(viewed.stdout)
	except json.JSONDecodeError as exc:
		raise ReleaseError(f"npm view emitted unparseable JSON: {exc}") from exc
	if not isinstance(report, dict):
		raise ReleaseError(f"npm view returned {report!r}, not dist-tags and versions")
	versions = report.get("versions") or []
	versions = [versions] if isinstance(versions, str) else versions
	latest = (report.get("dist-tags") or {}).get("latest")
	if latest is not None and release_key(latest) is None:
		raise ReleaseError(f"the registry's latest tag names {latest!r}, which is not a plain release version")
	candidates = [v for v in [*versions, latest] if release_key(v) is not None]
	return max(candidates, key=release_key) if candidates else None


def dist_tag_for(version, newest):
	"""None (npm's default, `latest`) for a new newest release, else BACKFILL_TAG."""
	if release_key(version) is None:
		raise ReleaseError(f"package.json version {version!r} is not a plain release version")
	if newest is None or release_key(version) > release_key(newest):
		return None
	return BACKFILL_TAG


def publish(npm="npm", dist_tag=None):
	# No --tag for a newest release: npm's default is `latest`, and leaving it
	# implicit keeps npm 11's own refusal as a second check behind this one.
	published = run([npm, "publish", "--access", "public", *(["--tag", dist_tag] if dist_tag else [])])
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
		newest = newest_release(args.npm)
		dist_tag = dist_tag_for(version, newest)
		channel = f"dist-tag {dist_tag}, leaving latest on {newest}" if dist_tag else "dist-tag latest"
		if args.dry_run:
			print(f"would publish {PACKAGE}@{version} under {channel}", flush=True)
			return 0

		print(f"publishing {PACKAGE}@{version} under {channel}", flush=True)
		publish(args.npm, dist_tag)
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
