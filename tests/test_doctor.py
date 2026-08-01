"""scripts/doctor.py: the wiring self-check. Stdlib unittest only.

Run: python3 -m unittest tests.test_doctor -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
DOCTOR_PY = os.path.join(PAYLOAD, "scripts", "doctor.py")
SESSION_START_PY = os.path.join(PAYLOAD, "hooks", "session-start.py")

HARNESS_ENV_VARS = ("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "CURSOR_PLUGIN_ROOT",
                    "CURSOR_VERSION")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in
                       HARNESS_ENV_VARS + ("LEOS_AGENT_LOCAL_PATH",)}
        for var in HARNESS_ENV_VARS:
            os.environ.pop(var, None)
        os.environ["LEOS_AGENT_LOCAL_PATH"] = os.path.join(self.tmp.name, "local")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_cli(self, *args, env_overrides=None):
        env = dict(os.environ)
        for var in HARNESS_ENV_VARS:
            env.pop(var, None)
        env["LEOS_AGENT_LOCAL_PATH"] = os.environ["LEOS_AGENT_LOCAL_PATH"]
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run([sys.executable, DOCTOR_PY, *args], env=env,
                              capture_output=True, text=True, timeout=30)


class TestJsonReport(DoctorCase):
    def test_json_carries_every_section(self):
        done = self.run_cli("--json", env_overrides={"CLAUDE_PLUGIN_ROOT": PAYLOAD})
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads(done.stdout)
        for key in ("harness", "payload", "bootstrap", "tiers", "local_state",
                    "memory", "skills", "breadcrumbs"):
            self.assertIn(key, data)

    def test_every_tier_resolves_from_config(self):
        done = self.run_cli("--json", env_overrides={"CLAUDE_PLUGIN_ROOT": PAYLOAD})
        tiers = json.loads(done.stdout)["tiers"]
        with open(os.path.join(PAYLOAD, "config", "models.json"), encoding="utf-8") as fh:
            expected = json.load(fh)["harnesses"]["claude"]
        for tier, value in expected.items():
            self.assertEqual(tiers[tier]["model"], value["model"])

    def test_reports_shipped_skills_from_disk(self):
        done = self.run_cli("--json", env_overrides={"CLAUDE_PLUGIN_ROOT": PAYLOAD})
        skills = json.loads(done.stdout)["skills"]
        on_disk = sorted(
            name for name in os.listdir(os.path.join(PAYLOAD, "skills"))
            if os.path.isfile(os.path.join(PAYLOAD, "skills", name, "SKILL.md"))
        )
        self.assertEqual(skills["shipped_portable"], on_disk)

    def test_claude_only_skills_are_listed_as_unavailable_elsewhere(self):
        done = self.run_cli("--json", env_overrides={"PLUGIN_ROOT": PAYLOAD})
        data = json.loads(done.stdout)
        self.assertEqual(data["harness"]["value"], "codex")
        self.assertIn("review-pr", data["skills"]["excluded_here"])
        self.assertNotIn("review-pr", data["skills"]["expected_here"])


class TestHarnessDetection(DoctorCase):
    def test_agrees_with_the_bootstrap_for_every_harness(self):
        """The anti-drift test: doctor must never grow a second, divergent
        implementation of the detection rules."""
        session_start = _load(SESSION_START_PY, "leo_session_start_probe")
        doctor = _load(DOCTOR_PY, "leo_doctor_probe")
        cases = [
            ({"CLAUDE_PLUGIN_ROOT": PAYLOAD}, "claude"),
            ({"PLUGIN_ROOT": PAYLOAD, "CLAUDE_PLUGIN_ROOT": PAYLOAD}, "codex"),
            ({"CURSOR_PLUGIN_ROOT": PAYLOAD}, "cursor"),
        ]
        for overrides, expected in cases:
            with self.subTest(env=overrides):
                for var in HARNESS_ENV_VARS:
                    os.environ.pop(var, None)
                os.environ.update(overrides)
                self.assertEqual(session_start._detect_harness(), expected)
                self.assertEqual(doctor._detect_harness()[0], expected)


class TestDegradation(DoctorCase):
    def test_human_report_renders_without_a_local_store(self):
        done = self.run_cli(env_overrides={"CLAUDE_PLUGIN_ROOT": PAYLOAD})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("leo doctor", done.stdout)
        self.assertIn("no store yet", done.stdout)

    def test_report_never_claims_the_policy_loaded(self):
        """Only the running agent can see its own context; a script that
        asserted this would be guessing, which is the failure doctor exists
        to catch."""
        done = self.run_cli(env_overrides={"CLAUDE_PLUGIN_ROOT": PAYLOAD})
        self.assertIn("cannot prove the policy", done.stdout)

    def test_breadcrumbs_are_labelled_as_history(self):
        local = os.environ["LEOS_AGENT_LOCAL_PATH"]
        os.makedirs(local, exist_ok=True)
        with open(os.path.join(local, "session-start.log"), "w", encoding="utf-8") as fh:
            fh.write("policy injection skipped: ValueError: boom\n")
        done = self.run_cli(env_overrides={"CLAUDE_PLUGIN_ROOT": PAYLOAD})
        self.assertIn("provenance unknown", done.stdout)
        self.assertIn("not this session", done.stdout)


if __name__ == "__main__":
    unittest.main()
