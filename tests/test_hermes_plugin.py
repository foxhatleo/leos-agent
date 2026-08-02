"""Hermes native plugin registration and hook behavior."""

import importlib.util
import json
import os
import tempfile
import threading
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO, "__init__.py")

# Minimum slack left under POLICY_LIMIT. The policy grows a paragraph at a
# time; without a floor it reaches the ceiling between one release and the
# next and quietly stops being injected on Hermes.
HEADROOM_FLOOR = 500

# Kept as a literal so a regression that re-registers one is caught by name,
# and checked against config below so the literal cannot go stale.
CLAUDE_ONLY_NAMES = ("attach-pr",)


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
        # The fail-open cases deliberately drive _breadcrumb, which resolves
        # its path from the environment at call time. Without this redirect the
        # suite appends to the developer's own ~/.leos-agent-local/ on every
        # run, which is how that log filled up with untimestamped test noise.
        cls._sandbox = tempfile.TemporaryDirectory(prefix="leo-hermes-")
        cls._saved_local = os.environ.get("LEOS_AGENT_LOCAL_PATH")
        os.environ["LEOS_AGENT_LOCAL_PATH"] = cls._sandbox.name
        cls.plugin = _load_plugin()
        cls.ctx = FakeContext()
        cls.plugin.register(cls.ctx)

    @classmethod
    def tearDownClass(cls):
        if cls._saved_local is None:
            os.environ.pop("LEOS_AGENT_LOCAL_PATH", None)
        else:
            os.environ["LEOS_AGENT_LOCAL_PATH"] = cls._saved_local
        cls._sandbox.cleanup()

    def test_registers_every_portable_skill_and_required_hooks(self):
        skill_root = os.path.join(REPO, "plugins", "leo", "skills")
        with open(os.path.join(REPO, "plugins", "leo", "config", "models.json"), encoding="utf-8") as fh:
            excluded = set(json.load(fh)["skills"]["exclude"]["hermes"])
        expected = sorted(
            name
            for name in os.listdir(skill_root)
            if os.path.isfile(os.path.join(skill_root, name, "SKILL.md"))
            and name not in excluded
        )
        self.assertEqual(sorted(self.ctx.skills), expected)
        self.assertEqual(
            set(self.ctx.hooks),
            {"pre_llm_call", "pre_tool_call", "transform_tool_result"},
        )

    def test_operational_skills_are_registered(self):
        """The parity gain, proved in-process rather than asserted about a
        manifest string. Hermes is the only harness whose registration runs
        here, so it is the one place this can be shown rather than inferred.
        """
        for name in ("review-pr", "resolve-ticket", "watch-review"):
            with self.subTest(skill=name):
                self.assertIn(name, self.ctx.skills)

    def test_non_portable_skills_are_never_registered(self):
        """Asserted by name, not derived — a derived expectation would
        happily absorb a regression that re-registered all of them."""
        for name in CLAUDE_ONLY_NAMES:
            with self.subTest(skill=name):
                self.assertNotIn(name, self.ctx.skills)

    def test_the_named_exclusions_cover_every_claude_only_skill(self):
        """The list above was three names when config had four: attach-pr was
        never asserted. Keeping the by-name assertion (for the reason its own
        docstring gives) while checking the list itself for completeness.
        """
        with open(os.path.join(REPO, "plugins", "leo", "config", "models.json"), encoding="utf-8") as fh:
            self.assertEqual(set(CLAUDE_ONLY_NAMES), set(json.load(fh)["skills"]["claudeOnly"]))

    def test_policy_reaches_hermes_as_a_skill(self):
        """Hermes accepts a pre_llm_call hook and never invokes it (upstream
        #2817, closed as not planned), so the policy has to arrive some other
        way or it never arrives at all. Registering using-leo as an ordinary
        skill is that other way; excluding it on the assumption the hook fires
        left Hermes with no policy whatsoever."""
        self.assertIn("using-leo", self.ctx.skills)

    def test_policy_context_has_growth_headroom(self):
        """Trip in CI on policy growth, not in production. The ceiling is a
        hard failure mode; this fails while there is still room to react."""
        context = self.plugin._render_policy()
        self.assertLess(
            len(context),
            int(self.plugin.POLICY_LIMIT * 0.9),
            f"policy at {len(context)} of {self.plugin.POLICY_LIMIT} — under 10% headroom",
        )

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


class TestHermesPolicyInjection(unittest.TestCase):
    """pre_llm_call is dead upstream, so the policy rides the first tool
    result instead. The invariant that matters most is that a tool result is
    never damaged: this hook can only ever append, and any doubt returns None.
    """

    def setUp(self):
        self.plugin = _load_plugin()
        self.plugin._INJECTED = set()
        self.plugin._INJECTED_UNKEYED = False
        self.plugin._PRIMARY_ALIVE = False
        self.hook = self.plugin._on_transform_tool_result

    def test_policy_rides_the_first_tool_result(self):
        out = self.hook(tool_name="terminal", result="total 4\nREADME.md", task_id="t1")
        self.assertTrue(out.startswith("total 4\nREADME.md"))
        self.assertIn("<leo-policy>", out)
        self.assertIn("moonshotai/kimi-k3", out)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", out)

    def test_injected_once_per_session(self):
        self.assertIsNotNone(self.hook(result="ok", task_id="t1"))
        self.assertIsNone(self.hook(result="ok", task_id="t1"))
        self.assertIsNotNone(self.hook(result="ok", task_id="t2"))

    def test_unkeyed_sessions_still_inject_exactly_once(self):
        self.assertIsNotNone(self.hook(result="ok", task_id=""))
        self.assertIsNone(self.hook(result="ok", task_id=""))
        self.assertIsNone(self.hook(result="ok", task_id=None))

    def test_never_swallows_a_tool_result(self):
        """The assertion that matters most. Over budget must yield None —
        not an empty string, not a truncated result — because the runtime
        reads None as 'leave the result alone'."""
        original = self.plugin.POLICY_LIMIT
        self.plugin.POLICY_LIMIT = 10
        try:
            self.assertIsNone(self.hook(result="important output", task_id="t1"))
        finally:
            self.plugin.POLICY_LIMIT = original

    def test_failed_context_does_not_claim_the_session(self):
        original = self.plugin._context
        self.plugin._context = lambda: (_ for _ in ()).throw(RuntimeError("memory unavailable"))
        try:
            self.assertIsNone(self.hook(result="important output", task_id="retry"))
        finally:
            self.plugin._context = original
        self.assertIsNotNone(self.hook(result="important output", task_id="retry"))

    def test_concurrent_calls_claim_a_session_once(self):
        barrier = threading.Barrier(8)
        results = []
        lock = threading.Lock()

        def invoke():
            barrier.wait()
            value = self.hook(result="unchanged", task_id="shared")
            with lock:
                results.append(value)

        workers = [threading.Thread(target=invoke) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        injected = [value for value in results if value is not None]
        self.assertEqual(len(injected), 1)
        self.assertTrue(injected[0].startswith("unchanged\n\n<leo-policy>"))

    def test_claims_are_never_evicted_after_256_sessions(self):
        for number in range(300):
            self.assertIsNotNone(self.hook(result="ok", task_id=f"session-{number}"))
        self.assertIsNone(self.hook(result="ok", task_id="session-0"))

    def test_non_string_results_are_left_alone(self):
        self.assertIsNone(self.hook(result=None, task_id="t1"))
        self.assertIsNone(self.hook(result={"data": 1}, task_id="t1"))

    def test_pre_tool_call_is_never_used_to_inject(self):
        """pre_tool_call's only model-visible return is a block directive, so
        injecting through it would mean denying the user's tool call and
        dressing the policy up as an error. Pinned so it is not 'improved'
        into that later."""
        out = self.plugin._on_pre_tool_call(
            tool_name="terminal", args={"command": "pwd", "cwd": REPO}
        )
        self.assertIsNone(out)
        blocked = self.plugin._on_pre_tool_call(
            tool_name="terminal", args={"command": "rm -rf /", "cwd": REPO}
        )
        self.assertNotIn("<leo-policy>", blocked["message"])

    def test_the_fallback_stands_down_once_the_primary_channel_fires(self):
        """If upstream ever wires pre_llm_call up, the policy must not arrive
        twice — and the primary channel must stay unbounded, or the policy
        would vanish after turn one."""
        first = self.plugin._on_pre_llm_call(user_message="hello")
        self.assertIn("context", first)
        # Unbounded: it supplies context on every call, not just the first.
        self.assertIn("context", self.plugin._on_pre_llm_call(user_message="again"))
        # And the fallback now stays out of the way.
        self.assertIsNone(self.hook(result="ok", task_id="t9"))


if __name__ == "__main__":
    unittest.main()
