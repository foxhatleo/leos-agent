"""Behavioral tests for the npm publish gate.

This path runs once per tag and never in ordinary development, so the decisions
it makes — publish, skip, or refuse — are tested directly rather than exercised.
"""

import contextlib
import importlib.util
import io
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("publish_npm_test", ROOT / "scripts" / "publish-npm.py")
publish_npm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish_npm)


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["npm"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestPackGuard(unittest.TestCase):
    def test_build_residue_is_refused(self):
        inventory = ["LICENSE", "package.json", "scripts/check.py", "scripts/__pycache__/check.cpython-312.pyc"]
        self.assertEqual(
            publish_npm.forbidden_paths(inventory),
            ["scripts/__pycache__/check.cpython-312.pyc"],
        )
        with self.assertRaises(publish_npm.ReleaseError):
            publish_npm.check_inventory(inventory)

    def test_a_clean_tree_passes(self):
        publish_npm.check_inventory(["LICENSE", "package.json", "index.js", "rules/preferences.md"])

    def test_missing_license_is_refused(self):
        with self.assertRaises(publish_npm.ReleaseError):
            publish_npm.check_inventory(["package.json", "index.js"])

    def test_the_real_package_tree_is_clean(self):
        # The declared `files` allowlist must exclude residue on its own, since
        # CI runs the test suite — which writes __pycache__ — before publishing.
        publish_npm.check_inventory(publish_npm.pack_inventory())


class TestRegistryState(unittest.TestCase):
    def _with_view(self, result):
        original = publish_npm.run
        publish_npm.run = lambda command: result
        self.addCleanup(lambda: setattr(publish_npm, "run", original))

    def test_exact_version_present_is_a_noop(self):
        self._with_view(completed(0, stdout="10.1.0\n"))
        self.assertEqual(publish_npm.registry_state("10.1.0"), "present")

    def test_confirmed_404_allows_publishing(self):
        self._with_view(completed(1, stderr="npm error code E404\nnpm error 404 Not Found"))
        self.assertEqual(publish_npm.registry_state("10.1.0"), "absent")

    def test_an_ambiguous_failure_refuses_rather_than_publishing(self):
        # An auth failure or registry outage must never be read as "absent".
        self._with_view(completed(1, stderr="npm error code E401\nnpm error Unauthorized"))
        with self.assertRaises(publish_npm.ReleaseError):
            publish_npm.registry_state("10.1.0")

    def test_a_mismatched_lookup_refuses(self):
        self._with_view(completed(0, stdout="9.9.9\n"))
        with self.assertRaises(publish_npm.ReleaseError):
            publish_npm.registry_state("10.1.0")


class TestTagAgreement(unittest.TestCase):
    def test_declared_version_matches_package_json(self):
        expected = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        self.assertEqual(publish_npm.declared_version(), expected)

    def test_a_tag_that_disagrees_with_package_json_aborts(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = publish_npm.main(["--tag", "v0.0.1", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("does not match package.json", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
