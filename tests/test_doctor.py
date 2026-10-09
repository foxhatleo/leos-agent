import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import doctor
import settings_probe

OVERRIDES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "PI_CODING_AGENT_DIR", "OPENCODE_CONFIG_DIR",
             "OPENCODE_CONFIG", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
# Variables the doctor reads from the process environment; the test's own
# session may have any of them set.
PROBED = ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "CLAUDE_CODE_DISABLE_ADVISOR_TOOL",
          "ANTHROPIC_BASE_URL") + settings_probe.FLAG_FETCH_ANY_VALUE + settings_probe.FLAG_FETCH_WHEN_ON \
    + settings_probe.THIRD_PARTY_PROVIDERS


class DoctorCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.claude = self.root / "claude"
        self.local = self.root / "local"
        env = patch.dict(os.environ, {"HOME": str(self.root / "home"), "CLAUDE_CONFIG_DIR": str(self.claude),
                                      "LEOS_AGENT_LOCAL_PATH": str(self.local), "LEOS_AGENT_PRICE_REFRESH": "off"})
        env.start()
        self.addCleanup(env.stop)
        for name in OVERRIDES[1:] + PROBED:
            os.environ.pop(name, None)
        managed = patch.object(settings_probe, "MANAGED_DIRS", (str(self.root / "managed"),))
        managed.start()
        self.addCleanup(managed.stop)
        (self.root / "work").mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(str(self.root / "work"))

    def settings(self, data, path=None):
        path = path or self.claude / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path


class ReadOnlyDoctor(DoctorCase):
    def test_hermes_does_not_present_saved_tiers_as_applied(self):
        self.local.mkdir()
        (self.local / "routing.json").write_text('{"hermes":{"cheap":{"model":"haiku"}}}')
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root / "hermes")}):
            result = doctor.diagnose("hermes")
            stanza = doctor.routing.stanza("hermes", doctor.routing.load())
        self.assertEqual(result["tiers"]["status"], "unsupported")
        self.assertFalse(result["tiers"]["saved_mappings_applied"])
        self.assertNotIn("haiku", stanza)
        self.assertFalse((self.root / "hermes").exists())

    def test_clean_claude_check_does_not_create_config_or_claim_activation(self):
        result = doctor.diagnose("claude")
        self.assertTrue(result["installation_current"])
        self.assertIn("Not established", result["runtime_activation"])
        self.assertFalse(self.claude.exists())
        self.assertFalse(self.local.exists())
        self.assertEqual(result["issues"], [])
        self.assertIsNone(result["forced_subagent_model"])
        self.assertEqual(result["advisor"]["status"], "off")
        self.assertFalse(result["output_compression"]["configured"])

    def test_invalid_config_is_not_reported_healthy(self):
        self.local.mkdir()
        (self.local / "routing.json").write_text("{broken")
        result = doctor.diagnose("claude")
        self.assertTrue(result["issues"])
        self.assertFalse(result["installation_current"])
        self.assertFalse(result["routing_config"]["valid"])
        self.assertEqual(result["routing_config"]["ignored"], ["the whole file"])

    def test_an_invalid_routing_section_is_reported_ignored_and_the_rest_applies(self):
        self.local.mkdir()
        original = '{"claude": {"cheap": "sonnet"}, "codex": {"cheap": {"model": ""}}}'
        (self.local / "routing.json").write_text(original)
        result = doctor.diagnose("claude")
        self.assertEqual(result["routing_config"]["ignored"], ["codex"])
        self.assertTrue(any("not applied" in issue for issue in result["issues"]))
        self.assertEqual(result["tiers"]["cheap"]["requested"], "sonnet")
        self.assertEqual((self.local / "routing.json").read_text(), original)


class SubagentModel(DoctorCase):
    def test_force_in_settings_is_an_issue_naming_the_forced_model(self):
        path = self.settings({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
                                      "ANTHROPIC_API_KEY": "sk-secret-value"}})
        result = doctor.diagnose("claude")
        self.assertEqual(result["forced_subagent_model"], "haiku")
        self.assertEqual(result["subagent_model"]["force_set_in"], [{"scope": "user", "path": str(path)}])
        self.assertTrue(any("FORCE is on" in issue for issue in result["issues"]))
        self.assertNotIn("sk-secret", json.dumps(result))

    def test_force_without_a_model_pins_subagents_to_the_main_model(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "true"}):
            result = doctor.diagnose("claude")
        self.assertEqual(result["forced_subagent_model"], "inherit")
        self.assertIn("only the value 1", result["subagent_model"]["note"])

    def test_a_project_local_file_outranks_the_environment(self):
        self.settings({"env": {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "0"}}, self.root / "work" / ".claude" / "settings.local.json")
        with patch.dict(os.environ, {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"}):
            result = doctor.diagnose("claude")
        self.assertIsNone(result["forced_subagent_model"])
        self.assertEqual([s["scope"] for s in result["subagent_model"]["force_set_in"]], ["local", "environment"])

    def test_the_default_model_alone_is_a_note_not_an_issue(self):
        self.settings({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}})
        result = doctor.diagnose("claude")
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["subagent_model"]["model"], "haiku")
        self.assertIn("default only", result["subagent_model"]["note"])

    def test_managed_drop_ins_outrank_user_settings(self):
        self.settings({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "opus"}})
        self.settings({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}}, self.root / "managed" / "managed-settings.json")
        self.settings({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}},
                      self.root / "managed" / "managed-settings.d" / "20-models.json")
        result = doctor.diagnose("claude")
        self.assertEqual(result["subagent_model"]["model"], "haiku")


class GuardMode(DoctorCase):
    def test_effective_mode_and_unrecognised_values(self):
        for value, mode, recognised in (("off", "off", True), ("0", "off", True), (" Disabled ", "off", True),
                                        ("warn", "warn", True), ("", "on", True), ("of", "on", False)):
            with patch.dict(os.environ, {"LEOS_AGENT_DISPATCH_GUARD": value}):
                result = doctor.diagnose("codex")
            self.assertEqual((result["dispatch_guard"]["mode"], result["dispatch_guard"]["recognised"]),
                             (mode, recognised), value)
            self.assertEqual(any("DISPATCH_GUARD" in issue for issue in result["issues"]), not recognised, value)

    def test_claude_settings_env_outranks_the_shell(self):
        self.settings({"env": {"LEOS_AGENT_DISPATCH_GUARD": "warn"}})
        with patch.dict(os.environ, {"LEOS_AGENT_DISPATCH_GUARD": "off"}):
            self.assertEqual(doctor.diagnose("claude")["dispatch_guard"]["mode"], "warn")


class Advisor(DoctorCase):
    def test_configured_advisor_and_its_blockers(self):
        self.settings({"advisorModel": "opus"})
        self.assertEqual(doctor.diagnose("claude")["advisor"]["status"], "configured")
        with patch.dict(os.environ, {"DISABLE_TELEMETRY": "0"}):
            self.assertIn("DISABLE_TELEMETRY", doctor.diagnose("claude")["advisor"]["status"])
        with patch.dict(os.environ, {"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1"}):
            self.assertIn("disabled", doctor.diagnose("claude")["advisor"]["status"])
        self.assertIn("not involved", doctor.diagnose("claude")["advisor"]["dispatch_guard"])


class OutputCompression(DoctorCase):
    def rtk(self, matcher):
        return {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [{"type": "command", "command": "rtk hook claude"}]}]}}

    def test_rtk_in_user_settings_is_detected_and_its_reach_described(self):
        self.settings(self.rtk("Bash"))
        found = doctor.diagnose("claude")["output_compression"]
        self.assertTrue(found["configured"])
        self.assertEqual((found["found"][0]["rewrites_bash"], found["found"][0]["receives_dispatch_calls"]), (True, False))
        self.assertIn("Read, Grep and Glob bypass it", found["interaction"])

    def test_a_catch_all_rtk_matcher_is_flagged_as_receiving_dispatches(self):
        for matcher in ("*", "", "Bash|Agent", ".*"):
            self.settings(self.rtk(matcher))
            self.assertTrue(doctor.diagnose("claude")["output_compression"]["found"][0]["receives_dispatch_calls"], matcher)

    def test_rtk_from_an_installed_plugin_is_detected(self):
        plugin = self.root / "cache" / "rtk-plugin"
        self.settings({"hooks": self.rtk("Bash")["hooks"]}, plugin / "hooks" / "hooks.json")
        self.settings({"plugins": {"rtk@market": [{"scope": "user", "installPath": str(plugin)}]}},
                      self.claude / "plugins" / "installed_plugins.json")
        self.settings({"enabledPlugins": {"rtk@market": False}})
        found = doctor.diagnose("claude")["output_compression"]["found"]
        self.assertEqual((found[0]["scope"], found[0]["plugin"], found[0]["enabled"]), ("plugin", "rtk@market", False))

    def test_other_harness_locations(self):
        codex = self.root / "codex"
        self.settings({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "rtk hook codex"}]}]}},
                      codex / "hooks.json")
        hermes = self.root / "hermes"
        (hermes / "plugins" / "rtk-rewrite").mkdir(parents=True)
        with patch.dict(os.environ, {"CODEX_HOME": str(codex), "HERMES_HOME": str(hermes)}):
            self.assertTrue(settings_probe.output_compressors("codex")["configured"])
            self.assertTrue(settings_probe.output_compressors("hermes")["configured"])
            self.assertFalse(settings_probe.output_compressors("pi")["configured"])


class PriceFreshness(unittest.TestCase):
    def test_fetched_at_is_used_only_when_it_is_a_real_past_time(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": tmp}):
            now = time.time()
            self.assertEqual(doctor.price_freshness({"fetched_at": now - 3600, "models": []})["age_hours"], 1.0)
            old = doctor.price_freshness({"fetched_at": now - 30 * 86400, "models": []})
            self.assertTrue(old["stale"])
            self.assertEqual(old["catalog"], "bundled snapshot")
            for stamp in (None, "2026-09-08", True, now + 86400 * 3):
                result = doctor.price_freshness({"fetched_at": stamp, "models": []})
                self.assertIsNone(result["age_hours"], stamp)
                self.assertIsNone(result["stale"], stamp)

    def test_hand_amendments_are_reported_when_present(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": tmp}):
            now = time.time()
            result = doctor.price_freshness({"fetched_at": now - 30 * 86400, "amended_at": now - 7200,
                                             "amended_models": ["a/x", "a/y"], "models": []})
            self.assertEqual((result["amended_models"], result["amended_age_hours"]), (2, 2.0))
            self.assertIn("for 2 models", result["amended"])
            self.assertTrue(result["stale"], "freshness still describes the fetch")
            self.assertNotIn("amended", doctor.price_freshness({"fetched_at": now, "models": []}))
            self.assertNotIn("amended", doctor.price_freshness({"fetched_at": now, "amended_at": "soon", "models": []}))

    def test_a_refreshed_cache_is_named_as_the_source(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": tmp}):
            catalog = {"schema": 1, "source": "x", "fetched_at": time.time(), "models": []}
            Path(doctor.pricing.cache_path()).write_text(json.dumps(catalog))
            self.assertEqual(doctor.price_freshness(catalog)["catalog"], "local refresh cache")


if __name__ == "__main__":
    unittest.main()
