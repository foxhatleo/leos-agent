"""Release version gate and deterministic archive smoke tests."""

import os
import subprocess
import sys
import tarfile
import tempfile
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "tools", "release.py")
WORKFLOW = os.path.join(REPO, ".github", "workflows", "release.yml")


class TestRelease(unittest.TestCase):
    def test_version_gate_accepts_v4(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--check-version", "v4.0.0"],
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
                ["leo-4.0.0-hermes.tar.gz", "leo-4.0.0-plugin.tar.gz"],
            )
            for name in os.listdir(output):
                with self.subTest(archive=name):
                    with tarfile.open(os.path.join(output, name), "r:gz") as archive:
                        members = archive.getnames()
                    self.assertTrue(members)
                    self.assertTrue(all(member == "leo" or member.startswith("leo/") for member in members))
                    self.assertFalse(any("__pycache__" in member or member.endswith(".pyc") for member in members))

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
            for name in ("leo-4.0.0-hermes.tar.gz", "leo-4.0.0-plugin.tar.gz"):
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
        self.assertIn("gh release create", workflow)
        self.assertIn("claude plugin validate .", workflow)
        self.assertIn('validate_plugin.py" plugins/leo', workflow)
        self.assertIn("cursor/plugin-template", workflow)


if __name__ == "__main__":
    unittest.main()
