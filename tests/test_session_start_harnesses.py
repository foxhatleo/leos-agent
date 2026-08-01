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


# Same sandbox rationale as tests/test_session_start.py: the hook is a real
# subprocess, so it must never be pointed at the developer's own HOME.
_SANDBOX = tempfile.TemporaryDirectory(prefix="leo-session-start-harnesses-")

SANDBOX_ENV = {
    "HOME": _SANDBOX.name,
    "LEOS_AGENT_LOCAL_PATH": os.path.join(_SANDBOX.name, "local"),
    "CLAUDE_CONFIG_DIR": os.path.join(_SANDBOX.name, "claude"),
    "CODEX_HOME": os.path.join(_SANDBOX.name, "codex"),
    "XDG_CONFIG_HOME": os.path.join(_SANDBOX.name, "config"),
    "LEOS_AGENT_NO_PROJECT": "1",
}


def _run(env_overrides):
    env = dict(os.environ)
    for var in ROOT_ENV_VARS:
        env.pop(var, None)
    # A Codex-launched test run would otherwise leak CODEX_* into every case.
    for var in [k for k in env if k.startswith("CODEX_")]:
        env.pop(var, None)
    env.update(SANDBOX_ENV)
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
    """Codex sets BOTH PLUGIN_ROOT and CLAUDE_PLUGIN_ROOT — the latter on purpose,
    for compatibility with existing plugin hooks. Testing PLUGIN_ROOT alone is an
    environment Codex never produces, and asserting against it is what let Codex
    silently receive the Claude mapping in every real session."""

    def test_real_codex_env_yields_codex_mapping(self):
        result = _run({"PLUGIN_ROOT": PLUGIN, "CLAUDE_PLUGIN_ROOT": PLUGIN})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")

        payload = json.loads(result.stdout)
        hook_output = payload.get("hookSpecificOutput", {})
        self.assertEqual(hook_output.get("hookEventName"), "SessionStart")
        additional_context = hook_output.get("additionalContext", "")
        self.assertTrue(additional_context, "expected non-empty nested additionalContext")
        self.assertIn("<leo-policy>", additional_context)
        self.assertIn("# Codex mapping", additional_context)
        self.assertIn("gpt-5.6-sol", additional_context)
        self.assertNotIn("# Claude Code mapping", additional_context)

    def test_plugin_root_alone_still_detected_as_codex(self):
        result = _run({"PLUGIN_ROOT": PLUGIN})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        additional_context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("# Codex mapping", additional_context)


class TestClaudeStillGetsClaudeMapping(unittest.TestCase):
    """The Codex fix keys on PLUGIN_ROOT; Claude Code sets only its own prefixed
    variable, so this is the assertion that keeps the fix from stealing Claude."""

    def test_claude_only_env_yields_claude_mapping(self):
        result = _run({"CLAUDE_PLUGIN_ROOT": PLUGIN})
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        additional_context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("# Claude Code mapping", additional_context)
        self.assertNotIn("# Codex mapping", additional_context)


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
