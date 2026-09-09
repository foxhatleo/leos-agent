"""The diagnosis bundle is self-contained, internally consistent, and carries no prompt text.

The reader may not have this repository, so the archive must stand alone; the
JSON and its text rendering must come from one scan so the two never disagree
the way two runs a minute apart do; and a failing component is filed, not
fatal, because a bundle with one missing doctor file is still worth sending.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dispatch_log  # noqa: E402


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BundleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.root / "data"),
                                           "LEOS_AGENT_PRICE_REFRESH": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.bundle = load("usage_bundle_test", "usage_bundle.py")
        absent = {k: str(self.root / "absent" / k) for k in self.bundle.usage_scan.SOURCES}
        sources = mock.patch.dict(self.bundle.usage_scan.SOURCES, absent)
        sources.start()
        self.addCleanup(sources.stop)

    @staticmethod
    def fake_run(results):
        """A subprocess stand-in keyed by script basename or CLI name; unknown keys succeed."""
        def run(argv, **_kwargs):
            key = os.path.basename(str(argv[1])) if len(argv) > 1 and str(argv[1]).endswith(".py") else argv[0]
            default = (0, '{"ok": true}\n', "") if key.endswith(".py") else (0, "ok\n", "")
            code, out, err = results.get(key, default)
            return subprocess.CompletedProcess(argv, code, out, err)
        return run

    def build(self, *args, results=None):
        out = self.root / "bundle.zip"
        with mock.patch.object(self.bundle.subprocess, "run", self.fake_run(results or {})), \
                mock.patch("sys.stdout", new=io.StringIO()) as printed, \
                mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(self.bundle.main(list(args) + ["--out", str(out)]), 0)
        self.assertIn(str(out), printed.getvalue())
        return zipfile.ZipFile(out)


class TestContents(BundleCase):
    def test_the_archive_stands_alone(self):
        names = set(self.build("--since", "7d").namelist())
        for expected in ("README.md", "usage-7d.json", "usage-7d.txt", "environment.txt", "plugin.json",
                         "sources.md", "routing-show.txt", "guard-report.txt", "model-prices.json",
                         "scanner/usage_scan.py", "scanner/dispatch_log.py", "scanner/pricing.py"):
            self.assertIn(expected, names)
        for harness in self.bundle.routing.HARNESSES:
            self.assertIn("doctor-%s.json" % harness, names)
        self.assertFalse(any(n.endswith(".error.txt") for n in names))
        self.assertNotIn("dispatch.jsonl", " ".join(names))

    def test_json_and_text_come_from_the_same_run(self):
        archive = self.build("--since", "7d")
        report = json.loads(archive.read("usage-7d.json"))
        rendered = archive.read("usage-7d.txt").decode("utf-8")
        self.assertEqual(rendered, self.bundle.usage_scan.render(report) + "\n")

    def test_a_shorter_window_adds_the_seven_day_trend(self):
        archive = self.build("--since", "24h")
        names = archive.namelist()
        self.assertIn("usage-24h.json", names)
        self.assertIn("usage-7d.json", names)
        self.assertIn("trend context", archive.read("README.md").decode("utf-8"))
        # and the default window does not duplicate itself
        self.assertNotIn("trend context", self.build("--since", "7d").read("README.md").decode("utf-8"))

    def test_a_failed_component_is_filed_not_fatal(self):
        archive = self.build("--since", "7d", results={"doctor.py": (1, "", "doctor exploded\n")})
        names = archive.namelist()
        self.assertIn("doctor-claude.error.txt", names)
        self.assertNotIn("doctor-claude.json", names)
        self.assertIn("doctor exploded", archive.read("doctor-claude.error.txt").decode("utf-8"))
        self.assertIn("Components that failed", archive.read("README.md").decode("utf-8"))

    def test_a_not_current_installation_is_a_finding_not_a_failure(self):
        """doctor exits 1 when an installation is not current and still prints its
        report. Filing that as an error hid exactly the harnesses worth reading."""
        report = json.dumps({"harness": "x", "installation_current": False})
        archive = self.build("--since", "7d", results={"doctor.py": (1, report, "6 target(s) out of date\n")})
        names = archive.namelist()
        self.assertEqual(archive.read("doctor-claude.json").decode("utf-8"), report)
        self.assertIn("exit 1", archive.read("doctor-claude.exit.txt").decode("utf-8"))
        self.assertIn("out of date", archive.read("doctor-claude.exit.txt").decode("utf-8"))
        self.assertNotIn("doctor-claude.error.txt", names)
        self.assertNotIn("Components that failed", archive.read("README.md").decode("utf-8"))

    def test_environment_records_missing_clis_instead_of_failing(self):
        archive = self.build("--since", "7d", results={"claude": (127, "", "claude: not found on PATH")})
        env = archive.read("environment.txt").decode("utf-8")
        self.assertIn("claude_cli=claude: not found on PATH", env)
        self.assertIn("plugin_version=", env)
        self.assertIn("scan_window=--since 7d", env)

    def test_the_readme_headline_matches_the_json(self):
        archive = self.build("--since", "7d")
        readme = archive.read("README.md").decode("utf-8")
        report = json.loads(archive.read("usage-7d.json"))
        for name, data in report["harnesses"].items():
            if data.get("status") not in ("ok", "partial"):
                self.assertIn("- %s: %s" % (name, data["status"]), readme)


class TestPrivacy(BundleCase):
    def test_no_prompt_text_reaches_the_archive(self):
        """The bundle is for someone else's eyes. A brief, a transcript line, or a
        logged prompt head must not survive into any member."""
        secret = "BUNDLE_SECRET_MARKER"
        projects = self.root / "projects" / "-p"
        projects.mkdir(parents=True)
        (projects / "sess.jsonl").write_text(json.dumps({
            "type": "assistant", "requestId": "r1", "sessionId": "s1",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "message": {"id": "m1", "model": "claude-opus-5",
                        "usage": {"input_tokens": 5, "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0, "output_tokens": 1},
                        "content": [{"type": "tool_use", "name": "Agent",
                                     "input": {"subagent_type": "leo-standard", "prompt": "do " + secret}}]}}) + "\n")
        dispatch_log.append({"v": 2, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "harness": "claude",
                             "decision": "allow", "agent": "leo-standard", "prompt_text": secret})
        with mock.patch.dict(self.bundle.usage_scan.SOURCES, {"claude": str(self.root / "projects")}):
            archive = self.build("--since", "7d")
        for name in archive.namelist():
            self.assertNotIn(secret.encode("utf-8"), archive.read(name), name)
        # The scan still saw the dispatch: privacy came from what is emitted, not from scanning less.
        report = json.loads(archive.read("usage-7d.json"))
        self.assertEqual(report["routing"]["dispatches"], 1)


if __name__ == "__main__":
    unittest.main()
