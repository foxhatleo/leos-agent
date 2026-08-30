"""Behavioral tests for the cross-harness installer."""

import importlib.util
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent


def load_installer():
    spec = importlib.util.spec_from_file_location("leo_install_test", ROOT / "scripts" / "leo-install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def args(**overrides):
    values = {"dry_run": False, "uninstall": False, "check": False, "force": False, "writes": True}
    values.update(overrides)
    return types.SimpleNamespace(**values)


class TestManagedBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()
        cls.block = cls.installer.build_block(ROOT)

    def test_install_is_idempotent_and_uninstall_restores_user_text(self):
        original = "# My instructions\n\nKeep this.\n"
        installed = self.installer.inject(original, self.block)
        self.assertEqual(self.installer.inject(installed, self.block), installed)
        restored = self.installer.strip_block(installed).rstrip("\n") + "\n"
        self.assertEqual(restored, original)

    def test_fenced_example_is_not_a_real_marker(self):
        original = "```md\n<leos-agent>\nexample\n</leos-agent>\n```\n"
        installed = self.installer.inject(original, self.block)
        self.assertIn(original.strip(), installed)
        self.assertEqual(installed.count('<leos-agent version="'), 1)

    def test_malformed_markers_refuse_to_edit(self):
        with self.assertRaises(self.installer.BlockError):
            self.installer.inject("<leos-agent>\nno close\n", self.block)
        with self.assertRaises(self.installer.BlockError):
            self.installer.inject("</leos-agent>\n", self.block)

    def test_longer_fence_hides_shorter_fence_markers(self):
        # CommonMark: a ``` line inside a ```` fence is content, not a closer.
        # Markers inside such an example must not be treated as a live block.
        original = "````md\n```\n<leos-agent>\nexample\n</leos-agent>\n```\n````\n"
        installed = self.installer.inject(original, self.block)
        self.assertIn(original.strip(), installed)
        self.assertEqual(installed.count('<leos-agent version="'), 1)


class TestCodexPayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    def test_install_update_and_uninstall_cover_every_declared_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(self.installer.Path, "home", return_value=home):
                first = self.installer.run("codex", ROOT, args())
                second = self.installer.run("codex", ROOT, args())

                self.assertEqual([r.status for r in first], ["created", "created", "created"])
                self.assertEqual([r.status for r in second], ["unchanged", "unchanged", "unchanged"])
                for name in self.installer.CODEX_AGENTS:
                    installed = home / ".codex" / "agents" / f"{name}.toml"
                    source = ROOT / "payload" / "codex-agents" / f"{name}.toml"
                    self.assertEqual(installed.read_bytes(), source.read_bytes())

                removed = self.installer.run("codex", ROOT, args(uninstall=True))
                self.assertEqual([r.status for r in removed], ["removed", "removed", "removed"])
                self.assertFalse((home / ".codex" / "AGENTS.md").exists())
                for name in self.installer.CODEX_AGENTS:
                    self.assertFalse((home / ".codex" / "agents" / f"{name}.toml").exists())

    def test_foreign_agent_is_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".codex" / "agents" / "leo-runner.toml"
            target.parent.mkdir(parents=True)
            target.write_text("name = \"mine\"\n", encoding="utf-8")
            with mock.patch.object(self.installer.Path, "home", return_value=home):
                results = self.installer.run("codex", ROOT, args())
            by_target = {result.target: result for result in results}
            self.assertEqual(by_target["~/.codex/agents/leo-runner.toml"].status, "conflict")
            self.assertEqual(target.read_text(encoding="utf-8"), "name = \"mine\"\n")

    def test_codex_sources_carry_no_plugin_root_token(self):
        # The TOML copies are compared byte-for-byte against their sources in
        # the round-trip test above; a token appearing in one would make the
        # installed copy differ by design. Fail loudly here instead.
        for name in self.installer.CODEX_AGENTS:
            text = (ROOT / "payload" / "codex-agents" / f"{name}.toml").read_text(encoding="utf-8")
            self.assertNotIn(self.installer.PLUGIN_ROOT_TOKEN, text)


def isolated(installer, home):
    """Patches for a hermetic install: fake $HOME, empty machine-local config."""
    return (
        mock.patch.object(installer.Path, "home", return_value=home),
        mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(home / ".leos-agent-local")}),
    )


class TestOpenCodePayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    def run_opencode(self, home, **overrides):
        home_patch, env_patch = isolated(self.installer, home)
        with home_patch, env_patch:
            return self.installer.run("opencode", ROOT, args(**overrides))

    def test_round_trip_covers_every_copied_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first = self.run_opencode(home)
            self.assertTrue(all(r.status == "created" for r in first), [(r.target, r.status) for r in first])
            second = self.run_opencode(home)
            self.assertTrue(all(r.status == "unchanged" for r in second), [(r.target, r.status) for r in second])
            removed = self.run_opencode(home, uninstall=True)
            self.assertTrue(all(r.status == "removed" for r in removed), [(r.target, r.status) for r in removed])
            cfg = home / ".config" / "opencode"
            self.assertFalse((cfg / "skills" / "leo-install").exists())
            for name in self.installer.OPENCODE_SKILLS:
                self.assertFalse((cfg / "skills" / name).exists(), name)
            self.assertEqual(list((cfg / "commands").glob("*.md")), [])

    def test_copies_carry_absolute_root_and_no_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_opencode(home)
            token_in_some_copy = False
            for copy in sorted((home / ".config" / "opencode").rglob("*.md")):
                text = copy.read_text(encoding="utf-8")
                self.assertNotIn(self.installer.PLUGIN_ROOT_TOKEN, text, copy.name)
                # Prose may *mention* the env var; building a path from it is
                # the bug (same line test_policy draws for the sources).
                self.assertNotIn("CLAUDE_PLUGIN_ROOT}/", text, copy.name)
                if f"{ROOT}/scripts/" in text:
                    token_in_some_copy = True
            self.assertTrue(token_in_some_copy, "no copy embeds the absolute plugin root; substitution did not run")

    def test_install_skill_is_renamed_to_leo_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_opencode(home)
            copied = (home / ".config" / "opencode" / "skills" / "leo-install" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("name: leo-install\n", copied)
            self.assertNotIn("name: install\n", copied)
            self.assertIn("disable-model-invocation: true", copied)
            source = (ROOT / "skills" / "install" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("name: install\n", source)

    def test_stale_root_copy_updates_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dest = home / ".config" / "opencode" / "skills" / "doctor" / "SKILL.md"
            dest.parent.mkdir(parents=True)
            src = ROOT / "skills" / "doctor" / "SKILL.md"
            dest.write_text(self.installer.opencode_payload(src, Path("/old/fake/root")), encoding="utf-8")
            results = self.run_opencode(home)
            by_target = {r.target: r for r in results}
            self.assertEqual(by_target["~/.config/opencode/skills/doctor/SKILL.md"].status, "updated")
            self.assertIn(f"{ROOT}/scripts/", dest.read_text(encoding="utf-8"))

    def test_foreign_skill_copy_conflicts_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dest = home / ".config" / "opencode" / "skills" / "doctor" / "SKILL.md"
            dest.parent.mkdir(parents=True)
            dest.write_text("# my own notes, nothing to do with the plugin\n", encoding="utf-8")
            results = self.run_opencode(home)
            by_target = {r.target: r for r in results}
            self.assertEqual(by_target["~/.config/opencode/skills/doctor/SKILL.md"].status, "conflict")
            self.assertIn("my own notes", dest.read_text(encoding="utf-8"))


class TestAllHarnessRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    def round_trip(self, harness, home):
        home_patch, env_patch = isolated(self.installer, home)
        with home_patch, env_patch:
            first = self.installer.run(harness, ROOT, args())
            second = self.installer.run(harness, ROOT, args())
            removed = self.installer.run(harness, ROOT, args(uninstall=True))
        return first, second, removed

    def test_every_harness_round_trips(self):
        starter = "# I am Hermes\n"
        for harness in self.installer.HARNESSES:
            with self.subTest(harness=harness):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    if harness == "hermes":
                        (home / ".hermes").mkdir(parents=True)
                        (home / ".hermes" / "SOUL.md").write_text(starter, encoding="utf-8")
                    first, second, removed = self.round_trip(harness, home)
                    for phase, results in (("install", first), ("reinstall", second), ("uninstall", removed)):
                        self.assertFalse(
                            any(r.failed for r in results),
                            f"{harness} {phase}: {[(r.target, r.status, r.detail) for r in results]}",
                        )
                    self.assertFalse(
                        any(r.changed for r in second),
                        f"{harness} reinstall wrote something: {[(r.target, r.status) for r in second]}",
                    )
                    if harness == "hermes":
                        self.assertEqual((home / ".hermes" / "SOUL.md").read_text(encoding="utf-8"), starter)

    def test_hermes_without_soul_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            home_patch, env_patch = isolated(self.installer, home)
            with home_patch, env_patch:
                results = self.installer.run("hermes", ROOT, args())
            self.assertEqual([r.status for r in results], ["skipped"])
            self.assertFalse((home / ".hermes").exists())


class TestCursorRoutingRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    def run_cursor(self, home, **overrides):
        home_patch, env_patch = isolated(self.installer, home)
        with home_patch, env_patch:
            return self.installer.run("cursor", ROOT, args(**overrides))

    def rule_path(self, home):
        return home / ".cursor" / "rules" / "leos-agent-routing.mdc"

    def write_config(self, home, body):
        local = home / ".leos-agent-local"
        local.mkdir(parents=True, exist_ok=True)
        (local / "routing.json").write_text(body, encoding="utf-8")

    def test_unconfigured_install_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            [result] = self.run_cursor(home)
            self.assertEqual(result.status, "skipped")
            self.assertFalse(self.rule_path(home).exists())

    def test_configured_install_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.write_config(home, '{"cursor": {"runner": "cheap-model"}}')
            [created] = self.run_cursor(home)
            self.assertEqual(created.status, "created")
            self.assertIn("cheap-model", self.rule_path(home).read_text(encoding="utf-8"))
            [second] = self.run_cursor(home)
            self.assertEqual(second.status, "unchanged")
            [removed] = self.run_cursor(home, uninstall=True)
            self.assertEqual(removed.status, "removed")
            self.assertFalse(self.rule_path(home).exists())

    def test_unconfiguring_takes_back_our_stale_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.write_config(home, '{"cursor": {"runner": "cheap-model"}}')
            self.run_cursor(home)
            self.write_config(home, "{}")
            [result] = self.run_cursor(home)
            self.assertEqual(result.status, "removed")
            self.assertFalse(self.rule_path(home).exists())

    def test_unconfigured_install_leaves_a_foreign_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rule = self.rule_path(home)
            rule.parent.mkdir(parents=True)
            rule.write_text("# somebody else's rule\n", encoding="utf-8")
            [result] = self.run_cursor(home)
            self.assertEqual(result.status, "skipped")
            self.assertEqual(rule.read_text(encoding="utf-8"), "# somebody else's rule\n")


class TestInstructionFileWriting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    def run_claude(self, home):
        home_patch, env_patch = isolated(self.installer, home)
        with home_patch, env_patch:
            return self.installer.run("claude", ROOT, args())

    def test_crlf_file_keeps_crlf_and_stays_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".claude" / "CLAUDE.md"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"# Mine\r\n\r\nKeep this.\r\n")
            [first] = self.run_claude(home)
            self.assertEqual(first.status, "updated")
            raw = target.read_bytes()
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"\n\n\n", raw.replace(b"\r\n", b"\n"))
            self.assertIn("Keep this.", raw.decode("utf-8"))
            [second] = self.run_claude(home)
            self.assertEqual(second.status, "unchanged")

    def test_symlinked_file_is_written_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            real = home / "dotfiles" / "CLAUDE.md"
            real.parent.mkdir(parents=True)
            real.write_text("# Mine\n", encoding="utf-8")
            link = home / ".claude" / "CLAUDE.md"
            link.parent.mkdir(parents=True)
            link.symlink_to(real)
            [result] = self.run_claude(home)
            self.assertEqual(result.status, "updated")
            self.assertTrue(link.is_symlink())
            self.assertIn('<leos-agent version="', real.read_text(encoding="utf-8"))
            self.assertIn("# Mine", real.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
