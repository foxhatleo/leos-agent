"""Cursor plugin packaging lint: .cursor-plugin/plugin.json and
hooks/hooks-cursor.json. Stdlib unittest only.

Run: python3 -m unittest tests.test_cursor_plugin -v
"""

import json
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURSOR_PLUGIN_DIR = os.path.join(REPO, ".cursor-plugin")
PLUGIN_JSON = os.path.join(CURSOR_PLUGIN_DIR, "plugin.json")
HOOKS_DIR = os.path.join(REPO, "hooks")
HOOKS_CURSOR_JSON = os.path.join(HOOKS_DIR, "hooks-cursor.json")

FORBIDDEN_ENTRY_KEYS = {"matcher", "type", "async"}


def _load_plugin():
    with open(PLUGIN_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _load_hooks():
    with open(HOOKS_CURSOR_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _hooks_root(data):
    return data.get("hooks", data)


class TestCursorPluginJson(unittest.TestCase):
    def test_valid_and_core_fields(self):
        data = _load_plugin()
        self.assertEqual(data.get("name"), "leo")
        self.assertEqual(data.get("displayName"), "leo")
        self.assertEqual(data.get("version"), "3.1.0")
        self.assertEqual(data.get("skills"), "./skills/")
        self.assertEqual(data.get("hooks"), "./hooks/hooks-cursor.json")
        self.assertEqual(data.get("agents"), [])


class TestCursorPluginDirHasOnlyManifest(unittest.TestCase):
    def test_no_nested_component_dirs(self):
        entries = sorted(os.listdir(CURSOR_PLUGIN_DIR))
        self.assertEqual(entries, ["plugin.json"])
        self.assertTrue(os.path.isfile(os.path.join(CURSOR_PLUGIN_DIR, "plugin.json")))


class TestHooksCursorJson(unittest.TestCase):
    def test_version(self):
        data = _load_hooks()
        self.assertEqual(data.get("version"), 1)

    def test_session_start_references_session_start_py(self):
        data = _load_hooks()
        session_start = _hooks_root(data).get("sessionStart", [])
        self.assertTrue(session_start, "expected sessionStart to be non-empty")
        entry = session_start[0]
        self.assertIn("session-start.py", entry.get("command", ""))

    def test_before_shell_execution_references_cursor_guard(self):
        data = _load_hooks()
        before_shell = _hooks_root(data).get("beforeShellExecution", [])
        self.assertTrue(before_shell, "expected beforeShellExecution to be non-empty")
        entry = before_shell[0]
        self.assertIn("cursor-guard.py", entry.get("command", ""))
        self.assertIsInstance(entry.get("timeout"), int)
        self.assertIs(entry.get("failClosed"), True)

    def test_entries_carry_no_matcher_type_async_keys(self):
        data = _load_hooks()
        root = _hooks_root(data)
        for hook_name in ("sessionStart", "beforeShellExecution"):
            for entry in root.get(hook_name, []):
                with self.subTest(hook=hook_name, entry=entry):
                    self.assertFalse(set(entry.keys()) & FORBIDDEN_ENTRY_KEYS)

    def test_referenced_scripts_exist_and_executable(self):
        data = _load_hooks()
        root = _hooks_root(data)
        all_commands = [
            entry.get("command", "")
            for hook_name in ("sessionStart", "beforeShellExecution")
            for entry in root.get(hook_name, [])
        ]
        for command in all_commands:
            match = re.search(r"([\w./-]+\.py)", command)
            self.assertIsNotNone(match, f"no .py script found in command {command!r}")
            script_name = os.path.basename(match.group(1))
            path = os.path.join(HOOKS_DIR, script_name)
            with self.subTest(script=script_name):
                self.assertTrue(os.path.isfile(path), f"missing {path}")
                self.assertTrue(os.access(path, os.X_OK), f"{path} is not executable")


if __name__ == "__main__":
    unittest.main()
