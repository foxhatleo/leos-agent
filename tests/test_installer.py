"""Behavioral tests for the cross-harness installer."""

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
