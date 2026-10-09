"""The advisor opt-in edits one key of Claude's settings and nothing else.

Leo's settings file carries env values, hooks and permissions that are not
this helper's business: every test asserts the rest of the file survives byte
for byte, and that no other value reaches the output.
"""
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_advisor  # noqa: E402
import settings_probe  # noqa: E402

OVERRIDES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "PI_CODING_AGENT_DIR", "OPENCODE_CONFIG_DIR",
             "OPENCODE_CONFIG", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
SETTINGS = ('{\n    "env": {\n        "ANTHROPIC_API_KEY": "sk-secret-value"\n    },\n'
            '    "permissions": {"allow": ["Bash(ls *)"]},\n    "model": "sonnet"\n}\n')


class AdvisorCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.claude = self.root / "claude"
        self.local = self.root / "local"
        env = mock.patch.dict(os.environ, {"HOME": str(self.root / "home"), "CLAUDE_CONFIG_DIR": str(self.claude),
                                           "LEOS_AGENT_LOCAL_PATH": str(self.local)})
        env.start()
        self.addCleanup(env.stop)
        for name in OVERRIDES[1:]:
            os.environ.pop(name, None)
        managed = mock.patch.object(settings_probe, "MANAGED_DIRS", (str(self.root / "managed"),))
        managed.start()
        self.addCleanup(managed.stop)
        (self.root / "work").mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(str(self.root / "work"))
        self.settings = self.claude / "settings.json"

    def run_cli(self, *argv):
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            code = claude_advisor.main(list(argv))
        return code, out.getvalue(), json.loads(out.getvalue())

    def write(self, text, mode=0o640):
        self.claude.mkdir(exist_ok=True)
        self.settings.write_bytes(text.encode("utf-8"))
        self.settings.chmod(mode)


class TestOptIn(AdvisorCase):
    def test_without_apply_nothing_is_written_and_no_other_value_is_shown(self):
        self.write(SETTINGS)
        code, raw, out = self.run_cli("set", "opus")
        self.assertEqual((code, out["before"], out["after"], out["applied"]), (0, None, "opus", False))
        self.assertEqual(self.settings.read_text(), SETTINGS)
        self.assertNotIn("sk-secret", raw)
        self.assertFalse(self.local.exists())

    def test_apply_adds_only_the_key_keeping_layout_mode_and_a_backup(self):
        self.write(SETTINGS)
        code, raw, out = self.run_cli("set", "opus", "--apply")
        self.assertEqual((code, out["applied"]), (0, True))
        text = self.settings.read_text()
        self.assertEqual(text, SETTINGS.replace('"model": "sonnet"\n', '"model": "sonnet",\n    "advisorModel": "opus"\n'))
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o640)
        backup = Path(out["backup"])
        self.assertEqual(backup.parent, self.local / "backups")
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        self.assertNotIn("sk-secret", raw)
        code, _, restored = self.run_cli("restore", str(backup))
        self.assertEqual((code, restored["restored_files"]), (0, 1))
        self.assertEqual(self.settings.read_text(), SETTINGS)

    def test_replace_and_remove_touch_one_key(self):
        cases = {
            '{\r\n  "advisorModel": "sonnet",\r\n  "model": "opus"\r\n}\r\n': '{\r\n  "model": "opus"\r\n}\r\n',
            '{\n  "a": 1,\n  "advisorModel": "sonnet",\n  "b": 2\n}\n': '{\n  "a": 1,\n  "b": 2\n}\n',
            '{\n  "a": 1,\n  "advisorModel": "sonnet"\n}\n': '{\n  "a": 1\n}\n',
            '{"advisorModel": "sonnet"}': "{}",
        }
        for before, after in cases.items():
            self.write(before)
            self.assertEqual(self.run_cli("off", "--apply")[0], 0)
            self.assertEqual(self.settings.read_bytes().decode(), after, before)
            self.write(before)
            self.assertEqual(self.run_cli("set", "opus", "--apply")[0], 0)
            self.assertEqual(self.settings.read_bytes().decode(), before.replace('"sonnet"', '"opus"'))

    def test_a_one_line_object_stays_on_one_line_and_crlf_is_kept(self):
        self.write('{"model": "opus"}')
        self.run_cli("set", "fable", "--apply")
        self.assertEqual(self.settings.read_text(), '{"model": "opus", "advisorModel": "fable"}')
        self.write('{\r\n  "model": "opus"\r\n}\r\n')
        self.run_cli("set", "fable", "--apply")
        self.assertEqual(self.settings.read_bytes(), b'{\r\n  "model": "opus",\r\n  "advisorModel": "fable"\r\n}\r\n')

    def test_a_symlinked_settings_file_is_written_through(self):
        real = self.root / "dotfiles" / "claude-settings.json"
        real.parent.mkdir()
        real.write_text('{"model": "opus"}\n')
        self.claude.mkdir()
        self.settings.symlink_to(real)
        self.run_cli("set", "opus", "--apply")
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(json.loads(real.read_text()), {"model": "opus", "advisorModel": "opus"})

    def test_refusals_write_nothing(self):
        code, _, out = self.run_cli("set", "opus", "--apply")
        self.assertEqual((code, out["status"]), (1, "refused"))
        self.assertFalse(self.claude.exists(), "the settings directory must never be created")
        for text in ('{"advisorModel": 3}', "{broken", '{"a": 1, "a": 2}'):
            self.write(text)
            self.assertEqual(self.run_cli("set", "opus", "--apply")[0], 1, text)
            self.assertEqual(self.settings.read_text(), text)
        self.write("{}")
        for bad in ("opus; rm -rf ~", "gpt-6", ""):
            self.assertEqual(self.run_cli("set", bad, "--apply")[0], 1, bad)
        self.assertEqual(self.settings.read_text(), "{}")

    def test_show_reports_where_it_is_set(self):
        self.write('{"advisorModel": "opus", "env": {"ANTHROPIC_API_KEY": "sk-secret-value"}}')
        code, raw, out = self.run_cli("show")
        self.assertEqual((code, out["model"], out["status"]), (0, "opus", "configured"))
        self.assertEqual(out["set_in"], [{"scope": "user", "path": str(self.settings)}])
        self.assertNotIn("sk-secret", raw)


class TestNotADispatch(unittest.TestCase):
    """The advisor is a server tool: Claude Code runs no hook for it. Even a
    hook event named after it must not read as a dispatch."""

    def test_the_claude_dispatch_matcher_does_not_cover_the_advisor(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
        self.assertTrue(any(settings_probe.matcher_covers(group.get("matcher"), "Agent") for group in hooks))
        self.assertFalse(any(settings_probe.matcher_covers(group.get("matcher"), "advisor") for group in hooks))

    def test_the_guard_treats_an_advisor_event_as_not_a_dispatch(self):
        import dispatch_guard
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"HOME": tmp, "LEOS_AGENT_LOCAL_PATH": tmp, "LEOS_AGENT_PRICE_REFRESH": "off"}):
            result = dispatch_guard.process({"tool_name": "advisor", "tool_input": {}, "hook_event_name": "PreToolUse"},
                                            "claude")
        self.assertEqual((result["action"], result["reason"]), ("allow", "not-a-dispatch"))


if __name__ == "__main__":
    unittest.main()
