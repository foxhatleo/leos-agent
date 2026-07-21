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
PLUGIN = os.path.join(REPO, "plugins", "leo")
SESSION_START_PY = os.path.join(PLUGIN, "hooks", "session-start.py")

REQUIRED_SUBSTRINGS = (
    "<leo-policy>",
    "Model routing",
    "${user_config.opus_model}",
    "Skill index",
    "failed twice on the same question",
    "Claude Code mapping",
)

# Deliberate raise from 12000: the policy body is now harness-neutral (tier
# names as role labels, no [1m]/Agent-tool/Workflow-tool specifics) and the
# injector appends the Claude harness mapping on top of it, so the combined
# payload (body + mapping) runs larger than the body alone did. 14000 guards
# against future creep of body+mapping together, not a fresh estimate.
MAX_ADDITIONAL_CONTEXT_LEN = 14000


def _run(plugin_root, model_options=None):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = plugin_root
    for tier, model in (model_options or {}).items():
        env[f"CLAUDE_PLUGIN_OPTION_{tier.upper()}_MODEL"] = model
    return subprocess.run(
        [sys.executable, SESSION_START_PY],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestSessionStartInjectsPolicy(unittest.TestCase):
    def test_payload_shape_and_content(self):
        result = _run(PLUGIN)
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

    def test_claude_model_options_are_substituted(self):
        result = _run(PLUGIN, {"opus": "opus[1m]", "sonnet": "sonnet[1m]"})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")

        additional_context = json.loads(result.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("opus[1m]", additional_context)
        self.assertIn("sonnet[1m]", additional_context)
        self.assertNotIn("${user_config.opus_model}", additional_context)


class TestSessionStartDegradesGracefully(unittest.TestCase):
    def test_empty_plugin_root_yields_empty_object(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            result = _run(empty_dir)

        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        payload = json.loads(result.stdout)
        self.assertEqual(payload, {})


if __name__ == "__main__":
    unittest.main()
