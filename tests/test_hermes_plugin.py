"""Hermes native plugin registration and hook behavior."""

import importlib.util
import os
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO, "__init__.py")

# Minimum slack left under POLICY_LIMIT. The policy grows a paragraph at a
# time; without a floor it reaches the ceiling between one release and the
# next and quietly stops being injected on Hermes.
HEADROOM_FLOOR = 500


def _load_plugin():
    spec = importlib.util.spec_from_file_location("leo_hermes_plugin", ENTRYPOINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.skills = {}
        self.hooks = {}

    def register_skill(self, name, path, description=""):
        self.skills[name] = (path, description)

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class TestHermesPlugin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()
        cls.ctx = FakeContext()
        cls.plugin.register(cls.ctx)

    def test_registers_every_skill_and_required_hooks(self):
        skill_root = os.path.join(REPO, "plugins", "leo", "skills")
        expected = sorted(
            name
            for name in os.listdir(skill_root)
            if os.path.isfile(os.path.join(skill_root, name, "SKILL.md"))
        )
        self.assertEqual(sorted(self.ctx.skills), expected)
        self.assertEqual(set(self.ctx.hooks), {"pre_llm_call", "pre_tool_call"})

    def test_policy_context_is_bounded_and_contains_hermes_models(self):
        result = self.ctx.hooks["pre_llm_call"](user_message="hello")
        self.assertEqual(set(result), {"context"})
        context = result["context"]
        self.assertLessEqual(len(context), self.plugin.POLICY_LIMIT)
        self.assertIn("moonshotai/kimi-k3", context)
        self.assertIn("z-ai/glm-5.2", context)
        self.assertIn("homogeneous", context)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", context)
        self.assertIn("plugins/leo/scripts/state.py", context)

    def test_policy_context_keeps_growth_headroom(self):
        """Trip a test, not production, when the policy grows.

        The budget is enforced at runtime by fail-open degradation, so an
        oversized policy silently stops being injected. This floor makes the
        approach visible while there is still room to act on it.
        """
        context = self.plugin._render_policy()
        headroom = self.plugin.POLICY_LIMIT - len(context)
        self.assertGreaterEqual(
            headroom,
            HEADROOM_FLOOR,
            f"only {headroom} chars left of the Hermes policy budget; trim the "
            f"policy or raise POLICY_LIMIT before adding more",
        )

    def test_policy_context_fails_open_when_over_budget(self):
        original = self.plugin.POLICY_LIMIT
        self.plugin.POLICY_LIMIT = 10
        try:
            self.assertIsNone(self.plugin._policy_context())
            self.assertIsNone(self.ctx.hooks["pre_llm_call"](user_message="hello"))
        finally:
            self.plugin.POLICY_LIMIT = original

    def test_guard_blocks_catastrophic_terminal_command(self):
        guard = self.ctx.hooks["pre_tool_call"]
        result = guard(tool_name="terminal", args={"command": "rm -rf /", "cwd": REPO})
        self.assertEqual(result["action"], "block")
        self.assertIn("bash-guard", result["message"])
        self.assertIsNone(guard(tool_name="terminal", args={"command": "pwd", "cwd": REPO}))
        self.assertIsNone(guard(tool_name="read_file", args={"path": "README.md"}))

    def test_guard_accepts_hermes_command_shapes(self):
        guard = self.ctx.hooks["pre_tool_call"]
        for tool_name, args in (
            ("bash", {"cmd": "rm -rf /", "cwd": REPO}),
            ("shell", {"command": "rm -rf ~", "cwd": REPO}),
            ("execute_command", {"command": "rm -rf $HOME", "cwd": REPO}),
        ):
            with self.subTest(tool_name=tool_name, args=args):
                result = guard(tool_name=tool_name, args=args)
                self.assertEqual(result["action"], "block")


if __name__ == "__main__":
    unittest.main()
