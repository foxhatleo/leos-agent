#!/usr/bin/env python3
"""Validate Leo release versions and build reproducible plugin archives."""

import argparse
import gzip
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "plugins" / "leo"
MANIFESTS = (
    PAYLOAD / ".claude-plugin" / "plugin.json",
    PAYLOAD / ".codex-plugin" / "plugin.json",
    PAYLOAD / ".cursor-plugin" / "plugin.json",
    PAYLOAD / "package.json",
)
PLUGIN_YAML = ROOT / "plugin.yaml"


def _yaml_version(path):
    """The Hermes manifest's version, without a YAML dependency.

    plugin.yaml is the one non-JSON version source: Hermes loads the repository
    root, so its manifest sits beside __init__.py rather than in the payload.
    The file is four flat keys and a comment, so a line match is exact here and
    keeps the release tooling on the standard library.
    """
    match = re.search(
        r'^version:\s*["\']?([^"\'\s]+)["\']?\s*$',
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise ValueError("plugin.yaml has no version")
    return match.group(1)


def versions():
    found = {path.relative_to(ROOT).as_posix(): json.loads(path.read_text())["version"] for path in MANIFESTS}
    found[PLUGIN_YAML.relative_to(ROOT).as_posix()] = _yaml_version(PLUGIN_YAML)
    return found


def release_version():
    found = versions()
    unique = set(found.values())
    if len(unique) != 1:
        detail = ", ".join(f"{path}={version}" for path, version in found.items())
        raise ValueError("manifest version drift: " + detail)
    return unique.pop()


def check_tag(tag):
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag)
    if not match:
        raise ValueError(f"release tag must be vX.Y.Z, got {tag!r}")
    version = release_version()
    if match.group(1) != version:
        raise ValueError(f"tag {tag} does not match manifest version {version}")


def _include(path):
    return not any(part == "__pycache__" for part in path.parts) and path.suffix != ".pyc" and path.name != ".DS_Store"


def _tar_filter(info):
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    # Normalize permission bits too: a locally `chmod +x`'d file (or one
    # checked out with different umask) would otherwise produce a
    # byte-different archive than the same tree built in CI.
    info.mode = 0o755 if info.mode & 0o111 else 0o644
    return info


def _write_archive(destination, entries):
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for source, arcname in entries:
                    if source.is_dir():
                        for path in sorted(source.rglob("*")):
                            if _include(path):
                                relative = path.relative_to(source)
                                archive.add(path, arcname=str(Path(arcname) / relative), filter=_tar_filter, recursive=False)
                    elif _include(source):
                        archive.add(source, arcname=arcname, filter=_tar_filter, recursive=False)


def build(output):
    version = release_version()
    output.mkdir(parents=True, exist_ok=True)
    _write_archive(output / f"leo-{version}-plugin.tar.gz", [(PAYLOAD, "leo")])


def stage_npm(destination):
    """Construct the npm manifest whitelist without changing the checkout."""
    if destination.exists():
        raise ValueError(f"npm staging destination already exists: {destination}")
    package = json.loads((PAYLOAD / "package.json").read_text(encoding="utf-8"))
    destination.mkdir(parents=True)
    for filename in ("package.json", "README.md", "LICENSE"):
        shutil.copy2(PAYLOAD / filename, destination / filename)
    for entry in package["files"]:
        relative = Path(entry.rstrip("/"))
        source = PAYLOAD / relative
        target = destination / relative
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log", ".DS_Store"),
            )
        else:
            shutil.copy2(source, target)
    package_json = destination / "package.json"
    package.pop("scripts", None)
    package_json.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")


def npm_inventory(destination):
    """Return the publish tree inventory, rejecting transient local files."""
    if not (destination / "LICENSE").is_file():
        raise ValueError("npm staging tree has no LICENSE")
    inventory = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file())
    forbidden = [path for path in inventory if "__pycache__" in path or path.endswith((".pyc", ".log")) or path == ".DS_Store"]
    if forbidden:
        raise ValueError("npm staging tree contains transient files: " + ", ".join(forbidden))
    return inventory


def _run(command):
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _npm_not_found(output):
    return "E404" in output or "404 Not Found" in output or "not found" in output.lower()


def publish_npm(package, version, npm="npm"):
    """Publish only after an exact version lookup or a confirmed npm 404."""
    viewed = _run([npm, "view", f"leos-agent@{version}", "version"])
    output = (viewed.stdout + viewed.stderr).strip()
    if viewed.returncode == 0:
        if output != version:
            raise ValueError(f"npm returned {output!r}, not exact version {version!r}")
        return "already-published"
    if not _npm_not_found(output):
        raise ValueError(f"npm version lookup failed without a confirmed not-found: {output}")
    # Path("./npm-stage") stringifies to "npm-stage", which npm interprets as
    # a registry package spec.  An absolute path is unambiguously a local tree.
    published = _run([npm, "publish", str(package.resolve()), "--access", "public"])
    if published.returncode:
        raise ValueError(f"npm publish failed: {(published.stdout + published.stderr).strip()}")
    return "published"


def _github_not_found(output):
    return "release not found" in output.lower() or "HTTP 404" in output


def sync_github_release(tag, dist, gh="gh"):
    """Upload to an existing release or create it only after a proven 404."""
    assets = sorted(str(path) for path in Path(dist).iterdir() if path.is_file())
    viewed = _run([gh, "release", "view", tag])
    if viewed.returncode == 0:
        uploaded = _run([gh, "release", "upload", tag, *assets, "--clobber"])
        if uploaded.returncode:
            raise ValueError(f"GitHub release upload failed: {(uploaded.stdout + uploaded.stderr).strip()}")
        return "uploaded"
    output = (viewed.stdout + viewed.stderr).strip()
    if not _github_not_found(output):
        raise ValueError(f"GitHub release lookup failed without a confirmed not-found: {output}")
    created = _run([gh, "release", "create", tag, *assets, "--generate-notes", "--verify-tag"])
    if created.returncode:
        raise ValueError(f"GitHub release creation failed: {(created.stdout + created.stderr).strip()}")
    return "created"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-version", metavar="TAG")
    parser.add_argument("--build", metavar="OUTPUT", type=Path)
    parser.add_argument("--stage-npm", metavar="OUTPUT", type=Path)
    parser.add_argument("--inventory", metavar="OUTPUT", type=Path)
    parser.add_argument("--publish-npm", nargs=2, metavar=("PACKAGE", "VERSION"))
    parser.add_argument("--npm-bin", default="npm")
    parser.add_argument("--sync-github-release", nargs=2, metavar=("TAG", "DIST"))
    parser.add_argument("--gh-bin", default="gh")
    args = parser.parse_args()
    if not any((args.check_version, args.build, args.stage_npm, args.publish_npm, args.sync_github_release)):
        parser.error("a release action is required")
    try:
        if args.check_version:
            check_tag(args.check_version)
        if args.build:
            build(args.build)
        if args.stage_npm:
            stage_npm(args.stage_npm)
            inventory = npm_inventory(args.stage_npm)
            if args.inventory:
                args.inventory.write_text("\n".join(inventory) + "\n", encoding="utf-8")
        elif args.inventory:
            parser.error("--inventory requires --stage-npm")
        if args.publish_npm:
            publish_npm(Path(args.publish_npm[0]), args.publish_npm[1], args.npm_bin)
        if args.sync_github_release:
            sync_github_release(args.sync_github_release[0], args.sync_github_release[1], args.gh_bin)
    except (OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"release validation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
