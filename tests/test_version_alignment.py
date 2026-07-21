"""Version-string alignment across all four harness manifests."""

import json
import os
import re
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
EXPECTED_VERSION = "4.0.0"

MANIFESTS = {
    "claude": os.path.join(PAYLOAD, ".claude-plugin", "plugin.json"),
    "codex": os.path.join(PAYLOAD, ".codex-plugin", "plugin.json"),
    "cursor": os.path.join(PAYLOAD, ".cursor-plugin", "plugin.json"),
}


def _versions():
    versions = {}
    for label, path in MANIFESTS.items():
        with open(path, encoding="utf-8") as fh:
            versions[label] = json.load(fh).get("version")
    with open(os.path.join(REPO, "plugin.yaml"), encoding="utf-8") as fh:
        match = re.search(r'^version:\s*["\']?([^"\'\s]+)', fh.read(), re.MULTILINE)
    versions["hermes"] = match.group(1) if match else None
    return versions


class TestVersionsPinnedToV4(unittest.TestCase):
    def test_each_manifest_is_v4(self):
        for label, version in _versions().items():
            with self.subTest(manifest=label):
                self.assertEqual(version, EXPECTED_VERSION)


class TestVersionsAlignedAcrossManifests(unittest.TestCase):
    def test_all_versions_equal(self):
        versions = _versions()
        self.assertEqual(len(set(versions.values())), 1, f"version strings diverge: {versions}")


if __name__ == "__main__":
    unittest.main()
