#!/usr/bin/env python3
"""Validate Leo release versions and build reproducible plugin archives."""

import argparse
import gzip
import json
from pathlib import Path
import re
import tarfile


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "plugins" / "leo"
MANIFESTS = (
    PAYLOAD / ".claude-plugin" / "plugin.json",
    PAYLOAD / ".codex-plugin" / "plugin.json",
    PAYLOAD / ".cursor-plugin" / "plugin.json",
)


def versions():
    found = {path.relative_to(ROOT).as_posix(): json.loads(path.read_text())["version"] for path in MANIFESTS}
    match = re.search(
        r'^version:\s*["\']?([^"\'\s]+)["\']?\s*$',
        (ROOT / "plugin.yaml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise ValueError("plugin.yaml has no version")
    found["plugin.yaml"] = match.group(1)
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
    _write_archive(
        output / f"leo-{version}-hermes.tar.gz",
        [
            (ROOT / "plugin.yaml", "leo/plugin.yaml"),
            (ROOT / "__init__.py", "leo/__init__.py"),
            (PAYLOAD, "leo/plugins/leo"),
        ],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-version", metavar="TAG")
    parser.add_argument("--build", metavar="OUTPUT", type=Path)
    args = parser.parse_args()
    if not args.check_version and not args.build:
        parser.error("one of --check-version or --build is required")
    try:
        if args.check_version:
            check_tag(args.check_version)
        if args.build:
            build(args.build)
    except (OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"release validation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
