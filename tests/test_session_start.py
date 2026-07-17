"""hooks/session-start.py behavior: policy injection payload shape and
content, plus graceful degradation when the plugin root is empty.
Stdlib unittest only.

Run: python3 -m unittest tests.test_session_start -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_START_PY = os.path.join(REPO, "hooks", "session-start.py")

REQUIRED_SUBSTRINGS = (
    "<leo-policy>",
    "Model routing",
    "[1m]",
    "Skill index",
    "failed twice on the same question",
)

MAX_ADDITIONAL_CONTEXT_LEN = 12000


def _run(plugin_root):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = plugin_root
    return subprocess.run(
        [sys.executable, SESSION_START_PY],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestSessionStartInjectsPolicy(unittest.TestCase):
    def test_payload_shape_and_content(self):
        result = _run(REPO)
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")

        payload = json.loads(result.stdout)
        hook_output = payload.get("hookSpecificOutput", {})
        self.assertEqual(hook_output.get("hookEventName"), "SessionStart")

        additional_context = hook_output.get("additionalContext", "")
        self.assertTrue(additional_context, "expected non-empty additionalContext")

        for substring in REQUIRED_SUBSTRINGS:
            with self.subTest(substring=substring):
                self.assertIn(substring, additional_context)

        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", additional_context)
        self.assertLess(len(additional_context), MAX_ADDITIONAL_CONTEXT_LEN)


class TestSessionStartDegradesGracefully(unittest.TestCase):
    def test_empty_plugin_root_yields_empty_object(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            result = _run(empty_dir)

        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        payload = json.loads(result.stdout)
        self.assertEqual(payload, {})


if __name__ == "__main__":
    unittest.main()
