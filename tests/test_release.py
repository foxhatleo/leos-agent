"""Behavioral tests for the npm publish gate.

This path runs once per tag and never in ordinary development, so the decisions
it makes — publish, skip, or refuse — are tested directly rather than exercised.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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
        publish_npm.check_inventory(sorted(publish_npm.REQUIRED_FILES))

    def test_missing_native_profile_breaks_install_and_is_refused(self):
        with self.assertRaisesRegex(publish_npm.ReleaseError, "agents/leo-standard.md"):
            publish_npm.check_inventory(publish_npm.REQUIRED_FILES - {"agents/leo-standard.md"})

    def test_missing_license_is_refused(self):
        with self.assertRaises(publish_npm.ReleaseError):
            publish_npm.check_inventory(["package.json", "index.js"])

    def test_the_real_package_tree_is_clean(self):
        # The declared `files` allowlist must exclude residue on its own, since
        # CI runs the test suite — which writes __pycache__ — before publishing.
        publish_npm.check_inventory(publish_npm.pack_inventory())

    def test_packaged_opencode_installer_round_trip(self):
        inventory = publish_npm.pack_inventory()
        with tempfile.TemporaryDirectory(prefix="leo packaged install ") as tmp:
            base = Path(tmp)
            package = base / "package"
            for name in inventory:
                target = package / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            env = {**os.environ, "LEOS_AGENT_ROOT": str(package),
                   "OPENCODE_CONFIG_DIR": str(base / "config"),
                   "OPENCODE_CONFIG": str(base / "config" / "opencode.jsonc"),
                   "LEOS_AGENT_LOCAL_PATH": str(base / "data"),
                   "LEOS_AGENT_PRICE_REFRESH": "off", "PYTHONDONTWRITEBYTECODE": "1"}
            command = [sys.executable, str(package / "scripts/leo-install.py"), "opencode"]
            for flags in ([], ["--check"], ["--uninstall"]):
                result = subprocess.run(command + flags, env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((base / "config" / "agents" / "leo-standard.md").exists())


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
    def test_an_accepted_upload_the_registry_has_not_served_yet_is_not_a_failure(self):
        """Once npm accepts the upload the release has happened. Reporting the
        registry's read lag as a failure marked three consecutive successful
        releases red, which is how a release signal stops being read."""
        with mock.patch.object(publish_npm, "pack_inventory", return_value=publish_npm.REQUIRED_FILES), \
             mock.patch.object(publish_npm, "registry_state", return_value="absent"), \
             mock.patch.object(publish_npm, "publish") as upload, \
             mock.patch.object(publish_npm.time, "sleep"), \
             contextlib.redirect_stderr(io.StringIO()) as err, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(publish_npm.main([]), 0)
        upload.assert_called_once()
        self.assertIn("published leos-agent@", out.getvalue())
        # It must not keep asserting a staging hold: propagation is what the
        # three red releases actually turned out to be.
        self.assertNotIn("staged packages", err.getvalue())
        self.assertIn("npm view", err.getvalue())

    def test_a_lookup_outage_after_publishing_is_reported_but_never_fails(self):
        with mock.patch.object(publish_npm, "pack_inventory", return_value=publish_npm.REQUIRED_FILES), \
             mock.patch.object(publish_npm, "registry_state",
                               side_effect=["absent", publish_npm.ReleaseError("auth")]), \
             mock.patch.object(publish_npm, "publish") as upload, \
             mock.patch.object(publish_npm.time, "sleep"), \
             contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(publish_npm.main([]), 0)
        upload.assert_called_once()
        self.assertIn("registry lookup failed", err.getvalue())

    def test_a_failed_upload_still_fails(self):
        """The one thing that must stay fatal."""
        with mock.patch.object(publish_npm, "pack_inventory", return_value=publish_npm.REQUIRED_FILES), \
             mock.patch.object(publish_npm, "registry_state", return_value="absent"), \
             mock.patch.object(publish_npm, "publish",
                               side_effect=publish_npm.ReleaseError("npm publish failed: 402")), \
             contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(publish_npm.main([]), 1)
        self.assertIn("npm publish failed", err.getvalue())

    def test_registry_propagation_does_not_retry_the_upload(self):
        with mock.patch.object(publish_npm, "pack_inventory", return_value=publish_npm.REQUIRED_FILES), \
             mock.patch.object(publish_npm, "registry_state", side_effect=["absent", "absent", "present"]), \
             mock.patch.object(publish_npm, "publish") as upload, \
             mock.patch.object(publish_npm.time, "sleep") as sleep, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(publish_npm.main([]), 0)
        upload.assert_called_once()
        sleep.assert_called_once_with(5)

    def test_verification_errors_are_not_reinterpreted_as_propagation(self):
        with mock.patch.object(publish_npm, "registry_state", side_effect=publish_npm.ReleaseError("auth")), \
             mock.patch.object(publish_npm.time, "sleep") as sleep:
            with self.assertRaises(publish_npm.ReleaseError):
                publish_npm.wait_for_public_version("12.2026090802.0")
        sleep.assert_not_called()

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
