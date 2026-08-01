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
# payload (body + mapping) runs larger than the body alone did.
#
# Raised again 14000 -> 16000 when the memory index joined the payload: the
# injector now appends policy + mapping + a bounded memory block. The memory
# block sizes itself to the room left under this number, so this guards creep
# of all three together, not a fresh estimate of any one of them.
MAX_ADDITIONAL_CONTEXT_LEN = 16000


# The hook runs as a real subprocess with a real environment, so anything it
# writes lands wherever that environment points. Before memory projection that
# only meant breadcrumbs in the developer's own ~/.leos-agent-local; once the
# hook projects into per-user memory files, an unsandboxed run would rewrite
# ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md every time the suite executes.
# Every subprocess gets a throwaway HOME and an explicit projection kill switch.
_SANDBOX = tempfile.TemporaryDirectory(prefix="leo-session-start-")

SANDBOX_ENV = {
    "HOME": _SANDBOX.name,
    "LEOS_AGENT_LOCAL_PATH": os.path.join(_SANDBOX.name, "local"),
    "CLAUDE_CONFIG_DIR": os.path.join(_SANDBOX.name, "claude"),
    "CODEX_HOME": os.path.join(_SANDBOX.name, "codex"),
    "XDG_CONFIG_HOME": os.path.join(_SANDBOX.name, "config"),
    "LEOS_AGENT_NO_PROJECT": "1",
}


def _run(plugin_root, extra_env=None):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = plugin_root
    for var in [k for k in env if k.startswith("CODEX_")] + ["PLUGIN_ROOT", "CURSOR_PLUGIN_ROOT"]:
        env.pop(var, None)
    env.update(SANDBOX_ENV)
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


class TestMemoryEnvelope(unittest.TestCase):
    """Memory rides in its own envelope so a broken store can never cost the
    session its policy, and so each can be measured against the budget alone."""

    def _run_with_store(self, facts):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        local = os.path.join(sandbox.name, "local")
        env = {"LEOS_AGENT_LOCAL_PATH": local, "LEOS_AGENT_NO_PROJECT": "1"}
        memory_py = os.path.join(PLUGIN, "scripts", "memory.py")
        for title, body in facts:
            done = subprocess.run(
                [sys.executable, memory_py, "write", "global", "preference", title],
                env={**os.environ, **env}, input=body,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
        return _run(PLUGIN, extra_env=env)

    def _context(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_empty_store_adds_no_envelope(self):
        context = self._context(self._run_with_store([]))
        self.assertIn("<leo-policy>", context)
        self.assertNotIn("<leo-memory>", context)

    def test_stored_fact_reaches_the_session(self):
        context = self._context(
            self._run_with_store([("Squash merge", "Leo squashes, never a merge commit.")])
        )
        self.assertIn("<leo-memory>", context)
        self.assertIn("Squash merge", context)
        self.assertLess(len(context), MAX_ADDITIONAL_CONTEXT_LEN)

    def test_unreadable_store_still_yields_the_policy(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        broken = os.path.join(sandbox.name, "local", "memory", "global")
        os.makedirs(broken)
        with open(os.path.join(broken, "junk.md"), "w", encoding="utf-8") as fh:
            fh.write("not a fact")
        result = _run(PLUGIN, extra_env={
            "LEOS_AGENT_LOCAL_PATH": os.path.join(sandbox.name, "local"),
            "LEOS_AGENT_NO_PROJECT": "1",
        })
        self.assertIn("Model routing", self._context(result))


if __name__ == "__main__":
    unittest.main()
