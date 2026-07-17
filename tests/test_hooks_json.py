"""hooks/hooks.json lint: SessionStart and PreToolUse wiring, referenced
scripts exist and are executable. Stdlib unittest only.

Run: python3 -m unittest tests.test_hooks_json -v
"""

import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS_DIR = os.path.join(REPO, "hooks")
HOOKS_JSON = os.path.join(HOOKS_DIR, "hooks.json")


def _load():
    with open(HOOKS_JSON, encoding="utf-8") as fh:
        return json.load(fh)


class TestSessionStartHook(unittest.TestCase):
    def test_matcher_and_command(self):
        data = _load()
        session_start = data.get("hooks", {}).get("SessionStart", [])
        self.assertTrue(session_start, "expected hooks.SessionStart to be non-empty")

        entry = session_start[0]
        self.assertEqual(entry.get("matcher"), "startup|clear|compact")

        hooks = entry.get("hooks", [])
        self.assertTrue(hooks, "expected SessionStart[0].hooks to be non-empty")
        hook = hooks[0]
        command = hook.get("command", "")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", command)
        self.assertIn("session-start.py", command)
        self.assertIs(hook.get("async"), False)


class TestPreToolUseHook(unittest.TestCase):
    def test_matcher_and_command(self):
        data = _load()
        pre_tool_use = data.get("hooks", {}).get("PreToolUse", [])
        self.assertTrue(pre_tool_use, "expected hooks.PreToolUse to be non-empty")

        entry = pre_tool_use[0]
        self.assertEqual(entry.get("matcher"), "Bash")

        hooks = entry.get("hooks", [])
        self.assertTrue(hooks, "expected PreToolUse[0].hooks to be non-empty")
        hook = hooks[0]
        command = hook.get("command", "")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", command)
        self.assertIn("bash-guard.py", command)
        self.assertIsInstance(hook.get("timeout"), int)


class TestReferencedScriptsExist(unittest.TestCase):
    def test_scripts_present_and_executable(self):
        for script in ("session-start.py", "bash-guard.py"):
            path = os.path.join(HOOKS_DIR, script)
            with self.subTest(script=script):
                self.assertTrue(os.path.isfile(path), f"missing {path}")
                self.assertTrue(os.access(path, os.X_OK), f"{path} is not executable")


if __name__ == "__main__":
    unittest.main()
