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
                    "CURSOR_VERSION", "LEOS_AGENT_HARNESS")


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
        for tier in ("fable", "opus", "sonnet", "haiku"):
            self.assertEqual(tiers[tier]["model"], expected[tier]["model"])

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

    def test_no_signal_reports_unknown_where_the_bootstrap_defaults_to_claude(self):
        """The one place the two are meant to disagree.

        session-start's final branch is a default for a hook that only runs on
        the three hook harnesses. doctor ships to five, so inheriting that
        default made it report `claude` on Hermes and OpenCode — with Claude's
        tier table and four Claude-only skills listed as available.
        """
        session_start = _load(SESSION_START_PY, "leo_session_start_nosig")
        doctor = _load(DOCTOR_PY, "leo_doctor_nosig")
        self.assertEqual(session_start._detect_harness(), "claude")
        harness, source = doctor._detect_harness()
        self.assertEqual(harness, "unknown")
        self.assertEqual(source, "no signal")

    def test_harness_argument_wins_over_detection(self):
        doctor = _load(DOCTOR_PY, "leo_doctor_arg")
        os.environ["CLAUDE_PLUGIN_ROOT"] = PAYLOAD
        for argv in (["--harness", "opencode"], ["--harness=opencode"]):
            with self.subTest(argv=argv):
                self.assertEqual(doctor._detect_harness(argv), ("opencode", "--harness"))

    def test_unknown_harness_argument_falls_back_rather_than_inventing(self):
        doctor = _load(DOCTOR_PY, "leo_doctor_bogus")
        os.environ["CLAUDE_PLUGIN_ROOT"] = PAYLOAD
        self.assertEqual(doctor._detect_harness(["--harness", "bogus"])[0], "claude")

    def test_hookless_harnesses_declare_themselves(self):
        doctor = _load(DOCTOR_PY, "leo_doctor_env")
        os.environ["LEOS_AGENT_HARNESS"] = "opencode"
        harness, source = doctor._detect_harness()
        self.assertEqual(harness, "opencode")
        self.assertIn("LEOS_AGENT_HARNESS", source)

    def test_registration_sites_export_the_declaration(self):
        """The env var is only useful if the two hookless harnesses set it."""
        with open(os.path.join(REPO, "__init__.py"), encoding="utf-8") as fh:
            self.assertIn('os.environ["LEOS_AGENT_HARNESS"] = "hermes"', fh.read())
        plugin_js = os.path.join(PAYLOAD, "adapters", "opencode", "plugin.js")
        with open(plugin_js, encoding="utf-8") as fh:
            self.assertIn("process.env.LEOS_AGENT_HARNESS = 'opencode'", fh.read())


class TestHookessHarnessReport(DoctorCase):
    """What the report actually says once the harness is known."""

    def test_opencode_gets_its_own_tiers_and_no_claude_only_skills(self):
        done = self.run_cli("--json", "--harness", "opencode")
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads(done.stdout)
        self.assertEqual(data["harness"]["value"], "opencode")
        self.assertEqual(data["tiers"]["opus"]["model"], "moonshotai/kimi-k3")
        self.assertNotIn("review-pr", data["skills"]["expected_here"])
        self.assertIn("review-pr", data["skills"]["excluded_here"])

    def test_unknown_harness_claims_no_tier_table(self):
        done = self.run_cli("--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads(done.stdout)
        self.assertEqual(data["harness"]["value"], "unknown")
        self.assertIsNone(data["tiers"]["opus"]["model"])
        self.assertNotIn("review-pr", data["skills"]["expected_here"])


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
