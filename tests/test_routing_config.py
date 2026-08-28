"""The machine-local routing config: defaults, validation, and idempotency.

The feature exists to save money, so the properties that matter are that an
absent config changes nothing, that a typo is loud rather than silently
expensive, and that installing twice writes nothing the second time.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
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


class WriteCase(RoutingCase):
    def run_cli(self, *argv):
        """routing.py main() against the throwaway data root. Returns stdout."""
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.data)}), \
             contextlib.redirect_stdout(buffer):
            self.routing.main(list(argv))
        return buffer.getvalue()

    def expect_refusal(self, *argv):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(*argv)
        return str(caught.exception)

    def raw(self):
        return json.loads((self.data / "routing.json").read_text(encoding="utf-8"))


class TestWriting(WriteCase):
    def test_reads_create_nothing_and_set_creates_the_file(self):
        # The data root must stay untouched by anything that only looks: a
        # config appearing because someone ran `show` would be a write nobody
        # asked for.
        self.run_cli("path")
        self.run_cli("show")
        self.run_cli("set", "--harness", "pi", "--runner", "m", "--dry-run")
        # Not even a lock file: reads and dry runs never enter the write path.
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), [])

        self.run_cli("set", "--harness", "pi", "--runner", "m")
        self.assertEqual(self.raw(), {"pi": {"runner": {"model": "m"}}})

    def test_set_preserves_other_harnesses_and_their_shorthand(self):
        # Writing load()'s output back would normalise every other harness's
        # entry as a side effect of touching one. Leo's file is his.
        self.write_config({"cursor": {"runner": "fast-1"}, "opencode": {"executor": {"model": "m"}}})
        self.run_cli("set", "--harness", "codex", "--runner", "gpt-x")
        after = self.raw()
        self.assertEqual(after["cursor"], {"runner": "fast-1"})
        self.assertEqual(after["opencode"], {"executor": {"model": "m"}})
        self.assertEqual(after["codex"], {"runner": {"model": "gpt-x"}})

    def test_set_replaces_a_role_wholesale_including_its_effort(self):
        # Merging within a role would leave a stale effort silently attached to
        # a model that was never chosen with it.
        self.run_cli("set", "--harness", "codex", "--runner", "gpt-x", "--runner-effort", "low")
        out = self.run_cli("set", "--harness", "codex", "--runner", "gpt-y")
        self.assertEqual(self.raw()["codex"]["runner"], {"model": "gpt-y"})
        self.assertIn("(was gpt-x effort=low)", out)

    def test_roles_are_written_independently(self):
        self.run_cli("set", "--harness", "cursor", "--runner", "a")
        self.run_cli("set", "--harness", "cursor", "--executor", "b")
        self.assertEqual(
            self.raw()["cursor"], {"runner": {"model": "a"}, "executor": {"model": "b"}}
        )

    def test_effort_needs_its_model_and_a_role_is_required(self):
        self.assertIn("--runner-effort needs --runner", self.expect_refusal(
            "set", "--harness", "codex", "--runner-effort", "low"))
        self.assertIn("needs --runner and/or --executor", self.expect_refusal(
            "set", "--harness", "codex"))
        self.assertFalse((self.data / "routing.json").exists())

    def test_an_unknown_harness_never_reaches_the_file(self):
        # argparse rejects it before anything is opened, so a typo cannot leave
        # a harness silently on the expensive model.
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("set", "--harness", "clod", "--runner", "x")
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.data / "routing.json").exists())

    def test_an_empty_model_is_rejected(self):
        self.assertIn("non-empty 'model'", self.expect_refusal(
            "set", "--harness", "cursor", "--runner", "  "))
        self.assertFalse((self.data / "routing.json").exists())

    def test_set_is_idempotent_and_byte_stable(self):
        # A re-run that rewrote the file would make the next install report a
        # change it did not make.
        self.run_cli("set", "--harness", "cursor", "--runner", "fast-1")
        before = (self.data / "routing.json").read_bytes()
        out = self.run_cli("set", "--harness", "cursor", "--runner", "fast-1")
        self.assertIn("unchanged", out)
        self.assertEqual((self.data / "routing.json").read_bytes(), before)

    def test_a_malformed_config_is_never_silently_rewritten(self):
        (self.data / "routing.json").write_text("{nope", encoding="utf-8")
        self.assertIn("not valid JSON", self.expect_refusal(
            "set", "--harness", "codex", "--runner", "gpt-x"))
        self.assertEqual((self.data / "routing.json").read_text(encoding="utf-8"), "{nope")

    def test_a_config_that_is_valid_json_but_not_a_config_is_refused(self):
        (self.data / "routing.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIn("expected an object keyed by harness", self.expect_refusal(
            "set", "--harness", "codex", "--runner", "gpt-x"))
        self.assertEqual((self.data / "routing.json").read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_an_existing_bad_key_blocks_the_write_rather_than_being_edited_around(self):
        # Writing anyway would leave the typo -- and the harness it silently
        # stranded on the expensive model -- in place.
        self.write_config({"clod": {"runner": "x"}})
        before = (self.data / "routing.json").read_bytes()
        self.assertIn("not a harness", self.expect_refusal(
            "set", "--harness", "codex", "--runner", "gpt-x"))
        self.assertEqual((self.data / "routing.json").read_bytes(), before)

    def test_unset_drops_a_role_then_the_harness_key(self):
        self.run_cli("set", "--harness", "cursor", "--runner", "a", "--executor", "b")
        self.run_cli("unset", "--harness", "cursor", "--runner")
        self.assertEqual(self.raw(), {"cursor": {"executor": {"model": "b"}}})
        self.run_cli("unset", "--harness", "cursor", "--executor")
        # An empty harness key is a shape load() never produces, so never leave one.
        self.assertEqual(self.raw(), {})

    def test_unset_without_a_role_drops_the_whole_harness(self):
        self.run_cli("set", "--harness", "pi", "--runner", "a", "--executor", "b")
        self.run_cli("set", "--harness", "cursor", "--runner", "keep-me")
        self.run_cli("unset", "--harness", "pi")
        self.assertEqual(self.raw(), {"cursor": {"runner": {"model": "keep-me"}}})

    def test_unset_with_no_config_writes_no_config(self):
        # The write path takes state.py's flock, so it leaves that lock
        # sentinel; what it must not do is conjure a config out of a no-op.
        out = self.run_cli("unset", "--harness", "hermes")
        self.assertIn("nothing configured", out)
        self.assertFalse((self.data / "routing.json").exists())

    def test_unset_says_what_the_harness_falls_back_to(self):
        # The one thing a reader can misjudge: on codex an unset restores a
        # shipped model, everywhere else it restores full price.
        self.run_cli("set", "--harness", "codex", "--runner", "gpt-x")
        self.run_cli("set", "--harness", "cursor", "--runner", "fast-1")
        self.assertIn("shipped default", self.run_cli("unset", "--harness", "codex"))
        self.assertIn("inherits the current model", self.run_cli("unset", "--harness", "cursor"))

    def test_every_harness_round_trips_from_the_writer_to_the_reader(self):
        for harness in self.routing.HARNESSES:
            with self.subTest(harness=harness):
                self.run_cli("set", "--harness", harness, "--runner", f"{harness}-m",
                             "--runner-effort", "low")
                entry = self.load_config()[harness]["runner"]
                self.assertEqual(entry, {"model": f"{harness}-m", "effort": "low"})

    def test_a_written_config_reaches_the_installed_payload(self):
        # The whole feature in one test: set, install, and the model is in the
        # file a session actually loads -- and a second install writes nothing.
        home = Path(self.tmp.name) / "home-write"
        home.mkdir()
        self.run_cli("set", "--harness", "cursor", "--runner", "fast-9")
        with mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.data)}), \
             mock.patch.object(Path, "home", staticmethod(lambda: home)):
            self.installer.run("cursor", ROOT, args())
            second = self.installer.run("cursor", ROOT, args())
        rule = home / ".cursor" / "rules" / "leos-agent-routing.mdc"
        self.assertIn("fast-9", rule.read_text())
        self.assertFalse([r.target for r in second if r.changed])


class TestSkillMatchesCLI(RoutingCase):
    """The skill drives this script by name; drift between them is silent."""

    def test_the_skill_only_names_subcommands_and_flags_the_script_has(self):
        parser_modes = {"show", "render", "path", "set", "unset"}
        sources = (
            ROOT / "skills" / "tune-routing" / "SKILL.md",
            ROOT / "skills" / "tune-routing" / "reference" / "harnesses.md",
            ROOT / "skills" / "doctor" / "SKILL.md",
        )
        known = {
            "show": {"--harness"},
            "render": {"--harness"},
            "path": set(),
            "set": {"--harness", "--runner", "--runner-effort", "--executor",
                    "--executor-effort", "--dry-run"},
            "unset": {"--harness", "--runner", "--executor", "--dry-run"},
        }
        seen = set()
        for path in sources:
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"routing\.py (\w[\w-]*)((?:[^\n`]|\\\n)*)", text):
                mode, tail = match.group(1), match.group(2)
                with self.subTest(source=path.name, mode=mode):
                    self.assertIn(mode, parser_modes)
                    seen.add(mode)
                    for flag in re.findall(r"--[a-z][a-z-]*", tail):
                        self.assertIn(flag, known[mode])
        self.assertTrue({"show", "set"} <= seen, "the skill stopped naming the script at all")


if __name__ == "__main__":
    unittest.main()
