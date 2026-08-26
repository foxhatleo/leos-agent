"""The Hermes entrypoint: plugin.yaml and __init__.py at the repository root.

Hermes is the one harness whose plugin root is this repository rather than the
payload, and the one whose registration is code rather than a manifest. So what
is checked here is what `register()` actually does when a real Hermes calls it:
which skills it hands over, which hooks it installs, and -- the point of the
file -- which hooks it does *not*.

Run: python3 -m unittest tests.test_hermes_plugin -v
"""

import importlib.util
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expected_version import EXPECTED_VERSION

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
ENTRYPOINT = os.path.join(REPO, "__init__.py")
PLUGIN_YAML = os.path.join(REPO, "plugin.yaml")
MODELS = os.path.join(PAYLOAD, "config", "models.json")

# Injection channels. 7.x rode transform_tool_result to put a multi-thousand
# token policy block into every session, which is why the harness was dropped in
# 8.0. Naming them here rather than asserting a bare hook count means a
# regression says what it broke.
INJECTION_HOOKS = ("pre_llm_call", "transform_tool_result")


def _entrypoint():
    """Load the root __init__.py by path, not by import.

    Importing it as a package would execute it as `leos-agent` the repository,
    which is not how Hermes loads it, and would depend on the checkout's
    directory name.
    """
    spec = importlib.util.spec_from_file_location("leo_hermes_entrypoint", ENTRYPOINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config():
    with open(MODELS, encoding="utf-8") as fh:
        return json.load(fh)


def _portable_skill_dirs():
    root = os.path.join(PAYLOAD, "skills")
    return {
        name
        for name in os.listdir(root)
        if os.path.isfile(os.path.join(root, name, "SKILL.md"))
    }


class _FakeContext:
    """Records what a real Hermes would have been handed."""

    def __init__(self):
        self.skills = {}
        self.hooks = []

    def register_skill(self, name, path):
        self.skills[name] = path

    def register_hook(self, event, handler):
        self.hooks.append((event, handler))


class TestManifest(unittest.TestCase):
    def test_manifest_declares_name_and_pinned_version(self):
        with open(PLUGIN_YAML, encoding="utf-8") as fh:
            manifest = fh.read()
        self.assertIn("name: leo", manifest)
        match = re.search(r'^version:\s*"([^"]+)"\s*$', manifest, re.MULTILINE)
        self.assertIsNotNone(match, "plugin.yaml has no quoted version")
        self.assertEqual(match.group(1), EXPECTED_VERSION)

    def test_manifest_declares_no_hooks(self):
        """Hooks reach Hermes through ctx.register_hook, never through the manifest."""
        with open(PLUGIN_YAML, encoding="utf-8") as fh:
            manifest = fh.read()
        body = "\n".join(
            line for line in manifest.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("hooks:", body)


class TestRegistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _entrypoint()

    def setUp(self):
        self.previous = os.environ.get("LEOS_AGENT_HARNESS")
        self.addCleanup(self._restore_env)
        self.ctx = _FakeContext()
        self.module.register(self.ctx)

    def _restore_env(self):
        if self.previous is None:
            os.environ.pop("LEOS_AGENT_HARNESS", None)
        else:
            os.environ["LEOS_AGENT_HARNESS"] = self.previous

    def test_it_registers_every_portable_skill(self):
        excluded = set(_config()["skills"]["exclude"]["hermes"])
        self.assertEqual(set(self.ctx.skills), _portable_skill_dirs() - excluded)

    def test_it_registers_no_claude_only_skill(self):
        """skills-claude/ is Claude-only by definition; the glob must not reach it."""
        claude_only = set(_config()["skills"]["claudeOnly"])
        self.assertTrue(claude_only, "fixture assumes at least one Claude-only skill")
        self.assertFalse(claude_only & set(self.ctx.skills))

    def test_every_registered_skill_path_exists(self):
        for name, path in self.ctx.skills.items():
            with self.subTest(skill=name):
                self.assertTrue(os.path.isfile(path))
                self.assertEqual(os.path.basename(str(path)), "SKILL.md")

    def test_it_registers_the_guard_and_nothing_else(self):
        self.assertEqual([event for event, _ in self.ctx.hooks], ["pre_tool_call"])

    def test_it_registers_no_injection_hook(self):
        """The 8.0 doctrine, pinned.

        Leo's policy reaches a Hermes session as skills the harness loads
        natively. If this fails, something has started injecting again.
        """
        registered = {event for event, _ in self.ctx.hooks}
        for event in INJECTION_HOOKS:
            with self.subTest(hook=event):
                self.assertNotIn(event, registered)

    def test_it_declares_the_harness_for_detection(self):
        """Hermes exports no plugin-root variable, so the entrypoint names it."""
        self.assertEqual(os.environ["LEOS_AGENT_HARNESS"], "hermes")


class TestGuardHook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # staticmethod: a bare function on a class attribute binds `self` into
        # the handler's first positional parameter, which is tool_name.
        cls.hook = staticmethod(_entrypoint()._on_pre_tool_call)

    def test_it_blocks_a_catastrophic_command(self):
        result = self.hook(tool_name="terminal", args={"command": "rm -rf ~"})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["action"], "block")
        self.assertIn("[bash-guard] BLOCKED", result["message"])

    def test_it_allows_a_routine_command(self):
        cwd = os.path.expanduser("~/project")
        for command in ("rm -rf ./build", "git status", "rm file.txt"):
            with self.subTest(command=command):
                self.assertIsNone(
                    self.hook(tool_name="terminal", args={"command": command, "cwd": cwd})
                )

    def test_it_covers_every_shell_tool_name_hermes_uses(self):
        for tool_name in ("terminal", "bash", "shell", "execute_command"):
            with self.subTest(tool_name=tool_name):
                result = self.hook(tool_name=tool_name, args={"command": "rm -rf /"})
                self.assertIsInstance(result, dict, f"{tool_name} was not guarded")
                self.assertEqual(result["action"], "block")

    def test_it_ignores_non_shell_tools(self):
        self.assertIsNone(self.hook(tool_name="read_file", args={"command": "rm -rf ~"}))

    def test_it_ignores_a_malformed_call(self):
        for args in (None, {}, {"command": ""}, {"command": 17}):
            with self.subTest(args=args):
                self.assertIsNone(self.hook(tool_name="terminal", args=args))

    def test_it_fails_closed_when_the_guard_raises(self):
        """A guard that fails open is not a guard."""
        module = _entrypoint()
        module._GUARD = type(
            "Exploding", (), {"check": staticmethod(lambda *a, **k: 1 / 0)}
        )()
        result = module._on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["action"], "block")
        self.assertIn("failing closed", result["message"])


if __name__ == "__main__":
    unittest.main()
