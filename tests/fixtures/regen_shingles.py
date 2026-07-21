#!/usr/bin/env python3
"""Regenerate tests/fixtures/superpowers_shingles.txt.

NOT a test — a one-off fixture generator, run by hand or by the
orchestrator, never by unittest discovery.

Reads SUPERPOWERS_PATH (default ~/workspace/superpowers), walks every
*.md file under its skills/ directory plus its top-level CLAUDE.md and
README.md, and hashes every 8-word shingle with the exact normalize()/
shingles() functions from tests/test_antiplagiarism.py — imported, not
reimplemented, so both sides of the plagiarism comparison agree by
construction. Writes the sorted, deduplicated set of hex digests to the
fixture, one per line. Idempotent: re-running against an unchanged
source tree reproduces byte-identical output.

Also walks SUPERPOWERS_PATH/hooks/** for script coverage. Those files are
hashed with shingles(text, strip_markdown=False) — "strip nothing" mode —
matching how Leo's hook scripts are scanned on the comparison side. The .md
sources keep the default strip_markdown=True pass.

Usage:
    python3 tests/fixtures/regen_shingles.py
    SUPERPOWERS_PATH=/path/to/superpowers python3 tests/fixtures/regen_shingles.py
"""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TESTS_DIR)
from test_antiplagiarism import shingles  # noqa: E402

FIXTURE_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURE_PATH = os.path.join(FIXTURE_DIR, "superpowers_shingles.txt")


def _source_paths(superpowers_path):
    """Markdown sources, hashed with the default strip_markdown=True pass."""
    paths = []
    skills_dir = os.path.join(superpowers_path, "skills")
    if os.path.isdir(skills_dir):
        for root, _dirs, files in os.walk(skills_dir):
            for f in files:
                if f.endswith(".md"):
                    paths.append(os.path.join(root, f))
    for name in ("CLAUDE.md", "README.md"):
        candidate = os.path.join(superpowers_path, name)
        if os.path.isfile(candidate):
            paths.append(candidate)
    return sorted(paths)


def _raw_source_paths(superpowers_path):
    """Hook scripts hashed with strip_markdown=False ("strip nothing")."""
    paths = []
    hooks_dir = os.path.join(superpowers_path, "hooks")
    if os.path.isdir(hooks_dir):
        for root, _dirs, files in os.walk(hooks_dir):
            for f in files:
                paths.append(os.path.join(root, f))
    return sorted(paths)


def main():
    superpowers_path = os.environ.get(
        "SUPERPOWERS_PATH", os.path.expanduser("~/workspace/superpowers")
    )
    md_paths = _source_paths(superpowers_path)
    raw_paths = _raw_source_paths(superpowers_path)
    if not md_paths and not raw_paths:
        print(f"no source files found under {superpowers_path}", file=sys.stderr)
        sys.exit(1)

    all_hashes = set()
    for path in md_paths:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        all_hashes |= shingles(text)
    for path in raw_paths:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        all_hashes |= shingles(text, strip_markdown=False)

    with open(FIXTURE_PATH, "w", encoding="utf-8") as fh:
        for digest in sorted(all_hashes):
            fh.write(digest + "\n")

    total_files = len(md_paths) + len(raw_paths)
    print(f"wrote {len(all_hashes)} shingle hashes from {total_files} files to {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
