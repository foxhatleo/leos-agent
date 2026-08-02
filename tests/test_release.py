"""Release version gate and deterministic archive smoke tests."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expected_version import EXPECTED_VERSION


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "tools", "release.py")
WORKFLOW = os.path.join(REPO, ".github", "workflows", "release.yml")


class TestRelease(unittest.TestCase):
    def test_version_gate_accepts_current_version(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--check-version", f"v{EXPECTED_VERSION}"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_version_gate_rejects_mismatch(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--check-version", "v4.0.1"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_archive_builder_outputs_both_packages(self):
        with tempfile.TemporaryDirectory() as output:
            result = subprocess.run(
                [sys.executable, SCRIPT, "--build", output],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                sorted(os.listdir(output)),
                [f"leo-{EXPECTED_VERSION}-hermes.tar.gz", f"leo-{EXPECTED_VERSION}-plugin.tar.gz"],
            )
            for name in os.listdir(output):
                with self.subTest(archive=name):
                    with tarfile.open(os.path.join(output, name), "r:gz") as archive:
                        members = archive.getnames()
                    self.assertTrue(members)
                    self.assertTrue(all(member == "leo" or member.startswith("leo/") for member in members))
                    self.assertFalse(any("__pycache__" in member or member.endswith(".pyc") for member in members))
                    self.assertFalse(any(member.endswith(".DS_Store") for member in members))
                    self.assertTrue(any("skills-claude/" in member for member in members))
                    self.assertTrue(any(member.endswith("/LICENSE") for member in members))

    def test_archive_builder_is_reproducible(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for output in (first, second):
                result = subprocess.run(
                    [sys.executable, SCRIPT, "--build", output],
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name in (f"leo-{EXPECTED_VERSION}-hermes.tar.gz", f"leo-{EXPECTED_VERSION}-plugin.tar.gz"):
                with self.subTest(archive=name):
                    with open(os.path.join(first, name), "rb") as lhs:
                        first_bytes = lhs.read()
                    with open(os.path.join(second, name), "rb") as rhs:
                        second_bytes = rhs.read()
                    self.assertEqual(first_bytes, second_bytes)

    def test_tag_triggered_workflow_publishes_built_archives(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            workflow = fh.read()
        self.assertIn("tags:", workflow)
        self.assertIn("v*", workflow)
        self.assertIn("tools/release.py --check-version", workflow)
        self.assertIn("tools/release.py --build", workflow)
        self.assertIn("--sync-github-release", workflow)
        self.assertIn("claude plugin validate .", workflow)
        self.assertIn("tools/vendor/codex/validate_plugin.py plugins/leo", workflow)
        self.assertIn("tools/vendor/cursor/validate-template.mjs", workflow)

    def test_stage_npm_creates_clean_publish_tree_without_mutating_source(self):
        """The publish input is staged; it must never clean the checkout."""
        cache = Path(REPO, "plugins", "leo", "__pycache__")
        cache.mkdir(exist_ok=True)
        token = uuid.uuid4().hex
        marker = cache / f"release-test-marker-{token}.pyc"
        marker.write_bytes(b"source cache must remain")
        self.addCleanup(marker.unlink, missing_ok=True)
        log_marker = Path(REPO, "plugins", "leo", f"release-test-marker-{token}.log")
        log_marker.write_text("source log must remain", encoding="utf-8")
        self.addCleanup(log_marker.unlink, missing_ok=True)

        with tempfile.TemporaryDirectory() as output:
            staged = Path(output, "leo")
            result = subprocess.run(
                [sys.executable, SCRIPT, "--stage-npm", str(staged)],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(marker.exists(), "staging must not delete source cache files")
            self.assertTrue((staged / "package.json").is_file())
            self.assertTrue((staged / "LICENSE").is_file())
            self.assertFalse((staged / "__pycache__").exists())
            self.assertFalse((staged / log_marker.name).exists())
            self.assertFalse((staged / "skills-claude").exists())
            self.assertFalse((staged / ".claude-plugin").exists())
            self.assertNotIn("prepack", (staged / "package.json").read_text(encoding="utf-8"))

    def test_stage_npm_writes_a_clean_package_tree_inventory(self):
        with tempfile.TemporaryDirectory() as output:
            staged = Path(output, "leo")
            inventory = Path(output, "inventory.txt")
            result = subprocess.run(
                [sys.executable, SCRIPT, "--stage-npm", str(staged), "--inventory", str(inventory)],
                cwd=REPO, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            contents = inventory.read_text(encoding="utf-8").splitlines()
            self.assertIn("LICENSE", contents)
            self.assertFalse(any("__pycache__" in path or path.endswith((".pyc", ".log")) for path in contents))
            npm = shutil.which("npm")
            if npm:
                packed = subprocess.run(
                    [npm, "pack", "--dry-run", "--json"], cwd=staged,
                    capture_output=True, text=True, timeout=30,
                    env=dict(os.environ, npm_config_cache=str(Path(output, "npm-cache"))),
                )
                self.assertEqual(packed.returncode, 0, packed.stdout + packed.stderr)
                files = {entry["path"] for entry in json.loads(packed.stdout)[0]["files"]}
                self.assertIn("LICENSE", files)
                self.assertIn("vendor/jsonc-parser-3.3.1/LICENSE.md", files)
                self.assertIn("vendor/jsonc-parser-3.3.1/README.md", files)
                self.assertIn("vendor/jsonc-parser-3.3.1/lib/umd/main.js", files)
                self.assertFalse(any(path.endswith((".pyc", ".log")) for path in files))

    def test_release_licenses_and_vendor_provenance_are_shipped(self):
        self.assertEqual(
            Path(REPO, "LICENSE").read_text(encoding="utf-8").splitlines()[0],
            "MIT License",
        )
        self.assertEqual(
            Path(REPO, "plugins", "leo", "LICENSE").read_text(encoding="utf-8").splitlines()[0],
            "MIT License",
        )
        provenance = Path(REPO, "tools", "vendor", "VALIDATORS.md").read_text(encoding="utf-8")
        self.assertIn("582569998181aad08a88bacc151a94b2048a5d1f", provenance)
        self.assertIn("46216072ac5750f782f95bb325b4d12b7c3ae9c9", provenance)
        self.assertIn("SHA-256", provenance)
        self.assertIn("Update procedure", provenance)
        self.assertIn("88fae0fd00998ea32fa2393869042f0231a2b43b", provenance)
        self.assertIn("independently authored", provenance.lower())
        self.assertIn("upstream repository declares no license", provenance.lower())
        self.assertNotIn("5310b9e8743213a7ac6c014d743bb03917dcf020", provenance)
        self.assertTrue(Path(REPO, "tools", "vendor", "codex", "validate_plugin.py").is_file())
        self.assertTrue(Path(REPO, "tools", "vendor", "cursor", "validate-template.mjs").is_file())
        codex_license = Path(REPO, "tools", "vendor", "codex", "LICENSE").read_text(
            encoding="utf-8"
        )
        self.assertIn("Apache License", codex_license)
        self.assertIn("Version 2.0, January 2004", codex_license)
        cursor_validator = Path(
            REPO, "tools", "vendor", "cursor", "validate-template.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Leo Liang", cursor_validator)
        self.assertIn("SPDX-License-Identifier: MIT", cursor_validator)

    def test_workflow_uses_pinned_validators_and_least_privilege_jobs(self):
        workflow = Path(WORKFLOW).read_text(encoding="utf-8")
        for action in (
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
            "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020",
        ):
            self.assertIn(action, workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("python3 tools/vendor/codex/validate_plugin.py plugins/leo", workflow)
        self.assertIn("node tools/vendor/cursor/validate-template.mjs", workflow)
        self.assertIn("@anthropic-ai/claude-code@2.1.220", workflow)
        self.assertIn("PyYAML==6.0.3", workflow)
        self.assertIn("npm@11.17.0", workflow)
        self.assertIn("--publish-npm ./npm-stage", workflow)
        self.assertIn("--sync-github-release", workflow)
        self.assertIn("tools/release.py --stage-npm", workflow)
        self.assertIn("license-inventory", workflow)

    def _fake_cli(self, root, name, body):
        path = Path(root, name)
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_publish_helpers_handle_exact_version_and_known_absence_only(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory, "calls")
            npm = self._fake_cli(directory, "npm", 'echo "$@" >> "' + str(log) + '"\ncase "$1" in\nview) printf "%s\\n" "7.0.0" ;;\n*) exit 9 ;;\nesac\n')
            result = subprocess.run([sys.executable, SCRIPT, "--publish-npm", "plugins/leo", "7.0.0", "--npm-bin", npm], cwd=REPO, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["view leos-agent@7.0.0 version"])

            npm = self._fake_cli(directory, "npm", 'printf "%s\\n" "7.0.1"\n')
            result = subprocess.run([sys.executable, SCRIPT, "--publish-npm", "plugins/leo", "7.0.0", "--npm-bin", npm], cwd=REPO, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

            npm = self._fake_cli(directory, "npm", 'echo "$@" >> "' + str(log) + '"\ncase "$1" in\nview) echo "npm ERR! code E404" >&2; exit 1 ;;\npublish) exit 0 ;;\nesac\n')
            result = subprocess.run([sys.executable, SCRIPT, "--publish-npm", "plugins/leo", "7.0.0", "--npm-bin", npm], cwd=REPO, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("publish plugins/leo --access public", log.read_text(encoding="utf-8"))

            npm = self._fake_cli(directory, "npm", 'echo "network unavailable" >&2\nexit 1\n')
            result = subprocess.run([sys.executable, SCRIPT, "--publish-npm", "plugins/leo", "7.0.0", "--npm-bin", npm], cwd=REPO, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_release_helper_uploads_existing_and_refuses_unknown_view_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory, "calls")
            dist = Path(directory, "dist")
            dist.mkdir()
            Path(dist, "asset.txt").write_text("asset", encoding="utf-8")
            gh = self._fake_cli(directory, "gh", 'echo "$@" >> "' + str(log) + '"\ncase "$2" in\nview) exit 0 ;;\nupload) exit 0 ;;\nesac\n')
            result = subprocess.run([sys.executable, SCRIPT, "--sync-github-release", "v7.0.0", str(dist), "--gh-bin", gh], cwd=REPO, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("release upload v7.0.0", log.read_text(encoding="utf-8"))
            self.assertIn("--clobber", log.read_text(encoding="utf-8"))

            gh = self._fake_cli(directory, "gh", 'echo "$@" >> "' + str(log) + '"\ncase "$2" in\nview) echo "release not found" >&2; exit 1 ;;\ncreate) exit 0 ;;\nesac\n')
            result = subprocess.run([sys.executable, SCRIPT, "--sync-github-release", "v7.0.0", str(dist), "--gh-bin", gh], cwd=REPO, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("release create v7.0.0", log.read_text(encoding="utf-8"))

            gh = self._fake_cli(directory, "gh", 'echo "network unavailable" >&2\nexit 1\n')
            result = subprocess.run([sys.executable, SCRIPT, "--sync-github-release", "v7.0.0", str(dist), "--gh-bin", gh], cwd=REPO, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
