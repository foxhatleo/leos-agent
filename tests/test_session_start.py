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


def _run(plugin_root, extra_env=None):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = plugin_root
    for var in [k for k in env if k.startswith("CODEX_")] + ["PLUGIN_ROOT", "CURSOR_PLUGIN_ROOT"]:
        env.pop(var, None)
    if extra_env:
        env.update(extra_env)
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

    def test_injected_mapping_names_concrete_models(self):
        """The mapping must name models, never a placeholder.

        Handing the model "${user_config.opus_model}" where a model name
        belongs is what broke every agent spawn in 4.0-5.0.0.
        """
        result = _run(PLUGIN)
        additional_context = json.loads(result.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        with open(os.path.join(PLUGIN, "config", "models.json"), encoding="utf-8") as fh:
            claude = json.load(fh)["harnesses"]["claude"]
        self.assertNotIn("${user_config.", additional_context)
        for tier, item in claude.items():
            with self.subTest(tier=tier):
                self.assertIn(item["model"], additional_context)


class TestSessionStartLocaleIndependent(unittest.TestCase):
    """The policy is full of em-dashes and arrows. Under a non-UTF-8 locale the
    platform default decoder raises, injection fails open, and the session runs
    with no policy at all — invisibly. LC_ALL=C is routine in cron, CI,
    containers, and plain ssh, so this is a real environment, not a synthetic one.
    """

    def test_c_locale_still_injects_policy(self):
        result = _run(PLUGIN, {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        payload = json.loads(result.stdout)
        additional_context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertTrue(
            additional_context,
            "policy vanished under LC_ALL=C — an open() is missing encoding='utf-8'",
        )
        self.assertIn("<leo-policy>", additional_context)


class TestSessionStartDegradesGracefully(unittest.TestCase):
    def test_empty_plugin_root_yields_empty_object(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            result = _run(empty_dir)

        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        payload = json.loads(result.stdout)
        self.assertEqual(payload, {})


if __name__ == "__main__":
    unittest.main()
