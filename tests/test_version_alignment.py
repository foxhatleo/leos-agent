"""Version-string alignment across every manifest the release gate reads.

All five sources are checked here, `package.json` and the root `plugin.yaml`
included. Before 8.0 this file covered only the three plugin manifests, so an
npm-only version drift could reach `tools/release.py` before anything caught it;
`plugin.yaml` returned as a source in 9.0 with the Hermes entrypoint.
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expected_version import EXPECTED_VERSION


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")

MANIFESTS = {
    "claude": os.path.join(PAYLOAD, ".claude-plugin", "plugin.json"),
    "codex": os.path.join(PAYLOAD, ".codex-plugin", "plugin.json"),
    "cursor": os.path.join(PAYLOAD, ".cursor-plugin", "plugin.json"),
    "npm": os.path.join(PAYLOAD, "package.json"),
}
# The one non-JSON source: Hermes loads the repository root, so its manifest
# sits beside __init__.py rather than in the payload.
PLUGIN_YAML = os.path.join(REPO, "plugin.yaml")
SOURCES = dict(MANIFESTS, hermes=PLUGIN_YAML)


def _yaml_version(path):
    with open(path, encoding="utf-8") as fh:
        match = re.search(r'^version:\s*["\']?([^"\'\s]+)["\']?\s*$', fh.read(), re.MULTILINE)
    return match.group(1) if match else None


def _versions():
    versions = {}
    for label, path in MANIFESTS.items():
        with open(path, encoding="utf-8") as fh:
            versions[label] = json.load(fh).get("version")
    versions["hermes"] = _yaml_version(PLUGIN_YAML)
    return versions


class TestVersionsPinned(unittest.TestCase):
    def test_each_manifest_matches_expected_version(self):
        for label, version in _versions().items():
            with self.subTest(manifest=label):
                self.assertEqual(version, EXPECTED_VERSION)


class TestVersionsAlignedAcrossManifests(unittest.TestCase):
    def test_all_versions_equal(self):
        versions = _versions()
        self.assertEqual(len(set(versions.values())), 1, f"version strings diverge: {versions}")

    def test_release_tool_reads_exactly_these_sources(self):
        """The gate and this test must not drift apart: the same five files.

        `tools/release.py` is what actually blocks a tagged release, so if it grows or
        loses a version source without this file following, the drift check silently
        stops covering it.
        """
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import release

        self.assertEqual(
            sorted(os.path.relpath(p, REPO).replace(os.sep, "/") for p in SOURCES.values()),
            sorted(release.versions()),
            "release.py version sources and this test's SOURCES have diverged",
        )


if __name__ == "__main__":
    unittest.main()
