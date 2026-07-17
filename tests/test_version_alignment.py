"""Version-string alignment across all four harness manifests. Stdlib
unittest only.

Run: python3 -m unittest tests.test_version_alignment -v

Note: .claude-plugin/plugin.json's version is owned by a different writer
in this v3.1 harness rollout. If it still reads "3.0.0" when this test
runs, the assertion below is expected to fail — that is a real gap to
close (the bump lands elsewhere), not a reason to weaken this test.
"""

import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED_VERSION = "3.1.0"

MANIFESTS = {
    "claude": os.path.join(REPO, ".claude-plugin", "plugin.json"),
    "codex": os.path.join(REPO, ".codex-plugin", "plugin.json"),
    "cursor": os.path.join(REPO, ".cursor-plugin", "plugin.json"),
    "opencode (package.json)": os.path.join(REPO, "package.json"),
}


def _version(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("version")


class TestVersionsPinnedTo_3_1_0(unittest.TestCase):
    def test_each_manifest_is_3_1_0(self):
        for label, path in MANIFESTS.items():
            with self.subTest(manifest=label):
                self.assertEqual(_version(path), EXPECTED_VERSION, f"{path} version mismatch")


class TestVersionsAlignedAcrossManifests(unittest.TestCase):
    def test_all_versions_equal(self):
        versions = {label: _version(path) for label, path in MANIFESTS.items()}
        distinct = set(versions.values())
        self.assertEqual(
            len(distinct), 1,
            f"version strings diverge across manifests: {versions}",
        )


if __name__ == "__main__":
    unittest.main()
