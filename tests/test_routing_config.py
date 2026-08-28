"""The machine-local routing config: defaults, validation, and idempotency.

The feature exists to save money, so the properties that matter are that an
absent config changes nothing, that a typo is loud rather than silently
expensive, and that installing twice writes nothing the second time.
"""

import importlib.util
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def args(**overrides):
    values = {"dry_run": False, "uninstall": False, "check": False, "force": False, "writes": True}
    values.update(overrides)
    return types.SimpleNamespace(**values)


class RoutingCase(unittest.TestCase):
    def setUp(self):
        self.routing = load("routing_test", "routing.py")
        self.installer = load("leo_install_routing_test", "leo-install.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()

    def write_config(self, payload):
        (self.data / "routing.json").write_text(json.dumps(payload), encoding="utf-8")

    def load_config(self):
        with mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.data)}):
            return self.routing.load()


class TestConfigLocation(RoutingCase):
    def test_config_lives_in_the_data_root_not_the_plugin(self):
        with mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.data)}):
            path = Path(self.routing.config_path())
        self.assertEqual(path.parent, self.data)
        self.assertNotIn(ROOT, path.parents)

    def test_absent_config_is_not_an_error(self):
        self.assertEqual(self.load_config(), {})


class TestValidation(RoutingCase):
    def assert_rejects(self, payload, needle):
        self.write_config(payload)
        with self.assertRaises(SystemExit) as caught:
            self.load_config()
        self.assertIn(needle, str(caught.exception))

    def test_unknown_harness_is_rejected(self):
        # Silently ignoring it would leave that harness on the expensive model,
        # which is the failure this config exists to remove.
        self.assert_rejects({"clod": {"runner": "x"}}, "not a harness")

    def test_unknown_role_and_field_are_rejected(self):
        self.assert_rejects({"cursor": {"runnr": "x"}}, "unknown key")
        self.assert_rejects({"cursor": {"runner": {"model": "x", "temperature": 1}}}, "unknown field")

    def test_empty_or_missing_model_is_rejected(self):
        self.assert_rejects({"cursor": {"runner": {"effort": "low"}}}, "non-empty 'model'")
        self.assert_rejects({"cursor": {"runner": "  "}}, "non-empty 'model'")

    def test_malformed_json_names_the_file(self):
        (self.data / "routing.json").write_text("{nope", encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            self.load_config()
        self.assertIn(str(self.data / "routing.json"), str(caught.exception))

    def test_model_strings_are_free_form(self):
        # Whatever the harness accepts, or whatever IT allowlisted, goes in
        # verbatim -- there is deliberately no known-model list to fall foul of.
        self.write_config({"opencode": {"runner": "some-vendor/an_odd.model:v3"}})
        self.assertEqual(
            self.load_config()["opencode"]["runner"]["model"], "some-vendor/an_odd.model:v3"
        )

    def test_bare_string_is_shorthand_for_a_model(self):
        self.write_config({"cursor": {"runner": "fast-1"}})
        self.assertEqual(self.load_config()["cursor"]["runner"], {"model": "fast-1", "effort": None})


class TestRendering(RoutingCase):
    def test_unconfigured_payload_is_unchanged_prose_for_every_harness(self):
        for harness in self.installer.HARNESSES:
            with self.subTest(harness=harness):
                body = self.installer.payload_body(ROOT, harness, {})
                self.assertNotIn(self.installer.ROUTING_OPEN, body)
                self.assertIn("Never upgrade a cheaper session", body)

    def test_rendering_is_smaller_than_the_unrendered_file(self):
        # Each machine stops carrying the other harnesses' model names. If this
        # ever inverts, the config has started costing what it was meant to save.
        raw = len(self.installer.payload_body(ROOT).encode("utf-8"))
        for harness in self.installer.HARNESSES:
            with self.subTest(harness=harness):
                self.assertLess(len(self.installer.payload_body(ROOT, harness, {}).encode("utf-8")), raw)

    def test_configured_models_reach_the_stanza(self):
        config = {"cursor": {"runner": {"model": "grok-code-fast-1", "effort": None}}}
        stanza = self.routing.stanza("cursor", config)
        self.assertIn("grok-code-fast-1", stanza)
        self.assertIn("leo-executor inherits", stanza)

    def test_roles_are_independent(self):
        config = {"pi": {"executor": {"model": "m", "effort": None}}}
        self.assertIn("leo-runner inherits", self.routing.stanza("pi", config))

    def test_claude_uses_a_model_override_not_a_new_agent_file(self):
        config = {"claude": {"runner": {"model": "haiku-x", "effort": None}}}
        stanza = self.routing.stanza("claude", config)
        self.assertIn('subagent_type: "leo-runner"', stanza)
        self.assertIn('model: "haiku-x"', stanza)

    def test_codex_toml_takes_the_configured_model_and_keeps_shipped_defaults(self):
        shipped = (ROOT / "payload" / "codex-agents" / "leo-runner.toml").read_text(encoding="utf-8")
        rendered = self.installer.render_codex_agent(
            shipped, "leo-runner", {"codex": {"runner": {"model": "gpt-x", "effort": None}}}
        )
        self.assertIn('model = "gpt-x"', rendered)
        # effort was not configured, so the profile keeps the one it ships with
        self.assertIn('model_reasoning_effort = "low"', rendered)
        self.assertEqual(self.installer.render_codex_agent(shipped, "leo-runner", {}), shipped)

    def test_rendering_is_deterministic(self):
        config = {"cursor": {"runner": {"model": "a", "effort": None}, "executor": {"model": "b", "effort": None}}}
        self.assertEqual(self.routing.stanza("cursor", config), self.routing.stanza("cursor", config))


class TestInstallIdempotency(RoutingCase):
    def install(self, harness, home):
        with mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.data)}), \
             mock.patch.object(Path, "home", staticmethod(lambda: home)):
            return self.installer.run(harness, ROOT, args())

    def test_second_install_writes_nothing_and_config_survives(self):
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        self.write_config({"codex": {"runner": {"model": "gpt-x", "effort": "minimal"}}})
        before = (self.data / "routing.json").read_bytes()

        first = self.install("codex", home)
        self.assertTrue(any(r.changed for r in first))
        second = self.install("codex", home)
        self.assertFalse([r.target for r in second if r.changed], "a second install rewrote a target")

        self.assertIn('model = "gpt-x"', (home / ".codex" / "agents" / "leo-runner.toml").read_text())
        self.assertIn('model_reasoning_effort = "minimal"', (home / ".codex" / "agents" / "leo-runner.toml").read_text())
        self.assertEqual((self.data / "routing.json").read_bytes(), before, "the installer wrote to the config")

    def test_uninstall_leaves_the_config_alone(self):
        home = Path(self.tmp.name) / "home2"
        home.mkdir()
        self.write_config({"cursor": {"runner": "fast-1"}})
        before = (self.data / "routing.json").read_bytes()
        self.install("cursor", home)
        rule = home / ".cursor" / "rules" / "leos-agent-routing.mdc"
        self.assertIn("fast-1", rule.read_text())

        with mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.data)}), \
             mock.patch.object(Path, "home", staticmethod(lambda: home)):
            self.installer.run("cursor", ROOT, args(uninstall=True))
        self.assertFalse(rule.exists())
        self.assertEqual((self.data / "routing.json").read_bytes(), before)

    def test_editing_the_config_makes_check_report_out_of_date(self):
        home = Path(self.tmp.name) / "home3"
        home.mkdir()
        self.install("codex", home)
        self.write_config({"codex": {"runner": {"model": "gpt-changed", "effort": None}}})
        with mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.data)}), \
             mock.patch.object(Path, "home", staticmethod(lambda: home)):
            results = self.installer.run("codex", ROOT, args(check=True, writes=False))
        self.assertTrue([r.target for r in results if r.changed])


if __name__ == "__main__":
    unittest.main()
