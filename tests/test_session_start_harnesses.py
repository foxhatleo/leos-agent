"""hooks/session-start.py multi-harness behavior: Cursor and Codex env-var
branches alongside the existing Claude Code shape, plus graceful degradation
for each when the plugin root is empty. Stdlib unittest only.

Run: python3 -m unittest tests.test_session_start_harnesses -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugins", "leo")
SESSION_START_PY = os.path.join(PLUGIN, "hooks", "session-start.py")

ROOT_ENV_VARS = ("CLAUDE_PLUGIN_ROOT", "CURSOR_PLUGIN_ROOT", "PLUGIN_ROOT")


def _run(env_overrides):
    env = dict(os.environ)
    for var in ROOT_ENV_VARS:
        env.pop(var, None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, SESSION_START_PY],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestCursorShape(unittest.TestCase):
    def test_cursor_plugin_root_yields_top_level_additional_context(self):
        result = _run({"CURSOR_PLUGIN_ROOT": PLUGIN})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")

        payload = json.loads(result.stdout)
        self.assertNotIn("hookSpecificOutput", payload)
        additional_context = payload.get("additional_context", "")
        self.assertTrue(additional_context, "expected non-empty top-level additional_context")
        self.assertIn("<leo-policy>", additional_context)
        self.assertIn("Grok 4.5", additional_context)


class TestCursorWinsWhenBothSet(unittest.TestCase):
    def test_cursor_shape_wins_over_claude(self):
        result = _run({"CURSOR_PLUGIN_ROOT": PLUGIN, "CLAUDE_PLUGIN_ROOT": PLUGIN})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")

        payload = json.loads(result.stdout)
        self.assertNotIn("hookSpecificOutput", payload)
        additional_context = payload.get("additional_context", "")
        self.assertTrue(additional_context, "expected non-empty top-level additional_context")
        self.assertIn("<leo-policy>", additional_context)
        self.assertIn("Grok 4.5", additional_context)


class TestCodexShape(unittest.TestCase):
    def test_plugin_root_yields_nested_hook_specific_output(self):
        result = _run({"PLUGIN_ROOT": PLUGIN})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")

        payload = json.loads(result.stdout)
        hook_output = payload.get("hookSpecificOutput", {})
        self.assertEqual(hook_output.get("hookEventName"), "SessionStart")
        additional_context = hook_output.get("additionalContext", "")
        self.assertTrue(additional_context, "expected non-empty nested additionalContext")
        self.assertIn("<leo-policy>", additional_context)
        self.assertIn("gpt-5.6-sol", additional_context)


class TestEmptyRootDegradesGracefully(unittest.TestCase):
    def test_cursor_empty_root_yields_empty_object(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            result = _run({"CURSOR_PLUGIN_ROOT": empty_dir})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        self.assertEqual(json.loads(result.stdout), {})

    def test_codex_empty_root_yields_empty_object(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            result = _run({"PLUGIN_ROOT": empty_dir})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        self.assertEqual(json.loads(result.stdout), {})

    def test_claude_empty_root_yields_empty_object(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            result = _run({"CLAUDE_PLUGIN_ROOT": empty_dir})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        self.assertEqual(json.loads(result.stdout), {})


if __name__ == "__main__":
    unittest.main()
