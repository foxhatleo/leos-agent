"""hooks/hooks.json lint: the Bash guard is wired, and nothing else is.

As of 8.0 the guard is the only hook Leo ships. Instructions reach the model
through natively loaded skills, so a SessionStart entry reappearing here is a
regression, not a feature — hence the negative assertions below.

Run: python3 -m unittest tests.test_hooks_json -v
"""

import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS_DIR = os.path.join(REPO, "plugins", "leo", "hooks")
HOOKS_JSON = os.path.join(HOOKS_DIR, "hooks.json")
HOOKS_CURSOR_JSON = os.path.join(HOOKS_DIR, "hooks-cursor.json")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class TestPreToolUseHook(unittest.TestCase):
    def test_matcher_and_command(self):
        pre_tool_use = _load(HOOKS_JSON).get("hooks", {}).get("PreToolUse", [])
        self.assertTrue(pre_tool_use, "expected hooks.PreToolUse to be non-empty")

        entry = pre_tool_use[0]
        self.assertEqual(entry.get("matcher"), "Bash")

        hooks = entry.get("hooks", [])
        self.assertTrue(hooks, "expected PreToolUse[0].hooks to be non-empty")
        hook = hooks[0]
        command = hook.get("command", "")
        self.assertIn("${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}", command)
        self.assertIn("$PLUGIN_ROOT", command)
        self.assertIn("bash-guard.py", command)
        self.assertIsInstance(hook.get("timeout"), int)
        # "async" is not a documented hooks-schema field; keep it out.
        self.assertNotIn("async", hook)


class TestNoPolicyInjectionHook(unittest.TestCase):
    """The 8.0 contract: the guard is the only hook.

    A SessionStart entry is how ~3,900 tokens per session came back before, so
    this is asserted rather than left to review.
    """

    def test_claude_manifest_declares_only_the_guard(self):
        hooks = _load(HOOKS_JSON).get("hooks", {})
        self.assertEqual(sorted(hooks), ["PreToolUse"])

    def test_cursor_manifest_declares_only_the_guard(self):
        hooks = _load(HOOKS_CURSOR_JSON).get("hooks", {})
        self.assertEqual(sorted(hooks), ["beforeShellExecution"])
        entry = hooks["beforeShellExecution"][0]
        self.assertIn("cursor-guard.py", entry.get("command", ""))
        self.assertIsInstance(entry.get("timeout"), int)
        # Cursor's guard fails closed; a shell command must not run because
        # the guard could not answer.
        self.assertIs(entry.get("failClosed"), True)

    def test_no_session_start_script_ships(self):
        self.assertFalse(
            os.path.exists(os.path.join(HOOKS_DIR, "session-start.py")),
            "hooks/session-start.py is back; policy injection was removed in 8.0",
        )


class TestReferencedScriptsExist(unittest.TestCase):
    def test_scripts_present_and_executable(self):
        for script in ("bash-guard.py", "cursor-guard.py"):
            path = os.path.join(HOOKS_DIR, script)
            with self.subTest(script=script):
                self.assertTrue(os.path.isfile(path), f"missing {path}")
                self.assertTrue(os.access(path, os.X_OK), f"{path} is not executable")


if __name__ == "__main__":
    unittest.main()
