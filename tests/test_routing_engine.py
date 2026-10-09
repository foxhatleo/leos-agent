import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pricing
import routing_engine as engine


class RoutingEngine(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads(pricing.BUNDLED.read_text())

    def route(self, harness, args, parent, **kw):
        tool = engine.CAPABILITIES[harness]["tools"][0]
        return engine.route(harness, tool, args, parent, config={}, catalog=self.catalog, **kw)

    def test_claude_missing_model_is_corrected_without_changing_other_args(self):
        args = {"subagent_type": "general-purpose", "prompt": "Investigate the failure", "run_in_background": True}
        result = self.route("claude", args, "opus")
        self.assertEqual(result["action"], "correct")
        self.assertEqual(result["updated_input"], dict(args, model="sonnet"))
        self.assertNotIn("model", args)

    def test_cheap_parent_cannot_be_upgraded_by_standard_default(self):
        result = self.route("claude", {"subagent_type": "leo-standard", "prompt": "Diagnose"}, "haiku")
        self.assertEqual(result["updated_input"]["model"], "haiku")

    def test_explicit_expensive_model_is_clamped(self):
        result = self.route("claude", {"subagent_type": "leo-cheap", "model": "opus"}, "sonnet")
        self.assertEqual(result["updated_input"]["model"], "sonnet")

    def test_claude_parent_transcript_id_becomes_a_valid_native_alias(self):
        result = self.route("claude", {"subagent_type": "leo-standard", "model": "sonnet"}, "claude-haiku-4-5-20251001")
        self.assertEqual(result["updated_input"]["model"], "haiku")

    def test_first_claude_dispatch_reports_missing_parent(self):
        result = self.route("claude", {"model": "sonnet"}, None)
        self.assertEqual(result["action"], "allow")
        self.assertEqual(result["reason"], "parent-model-unavailable")

    def test_unknown_explicit_model_is_allowed_unchanged(self):
        result = self.route("claude", {"model": "corporate-new-model"}, "haiku")
        self.assertEqual(result["action"], "allow")
        self.assertEqual(result["reason"], "price-unknown")
        self.assertIsNone(result["updated_input"])

    def test_codex_does_not_emit_unsupported_rewrite_response(self):
        result = self.route("codex", {"message": "Investigate"}, "gpt-6-astra")
        self.assertEqual(result["action"], "block")
        self.assertIn("gpt-6.1-sol", result["retry"])
        self.assertIsNone(result["updated_input"])

    def test_native_codex_profile_precedence_is_respected(self):
        result = self.route("codex", {"model": "gpt-5.6-luna"}, "gpt-5.6-sol", effective_model="gpt-5.6-terra")
        self.assertEqual(result["reason"], "profile-over-ceiling")

    def test_codex_cheap_profile_cannot_silently_use_expensive_parent(self):
        result = self.route("codex", {"agent_type": "leo-cheap", "model": "gpt-6-astra"}, "gpt-6-astra")
        self.assertEqual(result["action"], "block")
        self.assertIn("gpt-6-luna", result["retry"])

    def test_codex_standard_profile_is_capped_to_cheap_parent(self):
        result = self.route("codex", {"agent_type": "leo-standard", "model": "gpt-5.6-terra"}, "gpt-5.6-luna")
        self.assertEqual(result["action"], "block")
        self.assertIn("gpt-5.6-luna", result["retry"])
        retry = self.route("codex", {"agent_type": "leo-standard", "model": "gpt-5.6-luna"}, "gpt-5.6-luna")
        self.assertEqual(retry["action"], "allow")

    def test_codex_configured_effort_is_required_at_spawn(self):
        result = engine.route("codex", "spawn_agent", {"agent_type": "leo-cheap", "model": "gpt-5.6-luna"},
                              "gpt-6-astra", config={"codex": {"cheap": {"model": "gpt-5.6-luna", "effort": "low"}}}, catalog=self.catalog)
        self.assertEqual(result["action"], "block")
        self.assertIn("reasoning_effort='low'", result["retry"])

    def test_opencode_never_requests_a_nonexistent_model_field(self):
        result = self.route("opencode", {"subagent_type": "general", "prompt": "Investigate"}, "gpt-5.6-sol")
        self.assertEqual(result["action"], "allow")
        self.assertEqual(result["reason"], "unconfigured-native-profile")

    def test_hermes_control_action_is_not_a_spawn(self):
        result = self.route("hermes", {"action": "steer", "message": "Stop searching"}, "opus")
        self.assertEqual(result["reason"], "not-a-dispatch")

    def test_premium_defaults_and_parent_ceiling(self):
        claude = self.route("claude", {"subagent_type": "leo-premium"}, "opus")
        self.assertEqual(claude["updated_input"]["model"], "opus")
        capped = self.route("claude", {"subagent_type": "leo-premium"}, "haiku")
        self.assertEqual(capped["updated_input"]["model"], "haiku")
        codex = self.route("codex", {"agent_type": "leo-premium", "model": "gpt-6-astra"}, "gpt-6-astra")
        self.assertEqual(codex["action"], "allow")
        wrong = self.route("codex", {"agent_type": "leo-premium", "model": "gpt-6.1-sol"}, "gpt-6-astra")
        self.assertEqual(wrong["reason"], "tier-selection-required")
        self.assertIn("gpt-6-astra", wrong["retry"])
        capped = self.route("codex", {"agent_type": "leo-premium", "model": "gpt-6.1-sol"}, "gpt-6.1-sol")
        self.assertEqual(capped["action"], "allow")

    def test_only_owned_profiles_are_recognized(self):
        self.assertEqual(engine.tier_for("leos-agent:leo-runner"), "cheap")
        for name in ("leo-made-up", "other:leo-cheap", "path/leo-standard"):
            self.assertIsNone(engine.tier_for(name))


class ClaudeModelOrder(unittest.TestCase):
    """Claude runs a child on the call's `model`, else its definition's model,
    else the parent. Whatever the guard writes into `model` beats the agent's
    own definition, so it may only fill the field where the child inherits."""

    def setUp(self):
        self.catalog = json.loads(pricing.BUNDLED.read_text())

    def route(self, args, parent, config=None, catalog=None):
        return engine.route("claude", "Agent", dict(args, prompt="Investigate"), parent,
                            config=config or {}, catalog=catalog or self.catalog)

    def test_an_agent_that_pins_its_own_model_is_never_upgraded(self):
        result = self.route({"subagent_type": "other-plugin:haiku-scanner"}, "claude-opus-5-5")
        self.assertEqual((result["action"], result["reason"]), ("allow", "agent-defined-model"))
        self.assertIsNone(result["updated_input"])
        within = self.route({"subagent_type": "other-plugin:haiku-scanner", "model": "haiku"}, "claude-opus-5-5")
        self.assertEqual((within["action"], within["reason"]), ("allow", "within-ceiling"))

    def test_an_explicit_model_over_the_parent_is_still_clamped_for_any_agent(self):
        result = self.route({"subagent_type": "other-plugin:haiku-scanner", "model": "opus"}, "claude-sonnet-5-5")
        self.assertEqual(result["action"], "correct")
        self.assertEqual(result["updated_input"]["model"], "sonnet")

    def test_agents_that_inherit_get_the_standard_default(self):
        for agent in ("general-purpose", "claude", "Explore", "Plan", None):
            with self.subTest(agent=agent):
                args = {"subagent_type": agent} if agent else {}
                result = self.route(args, "claude-opus-5-5")
                self.assertEqual(result["updated_input"]["model"], "sonnet")

    def test_an_unknown_parent_fills_nothing_an_agent_would_inherit(self):
        for agent in ("general-purpose", "leos-agent:leo-parent", None):
            with self.subTest(agent=agent):
                result = self.route({"subagent_type": agent} if agent else {}, None)
                self.assertEqual((result["action"], result["reason"]), ("allow", "parent-model-unavailable"))
                self.assertIsNone(result["updated_input"])
        # A leo tier is pinned by its own definition; the configured tier model
        # is that same choice, so filling it cannot depend on the parent.
        cheap = self.route({"subagent_type": "leos-agent:leo-cheap"}, None)
        self.assertEqual(cheap["updated_input"]["model"], "haiku")

    def test_codex_parent_level_with_an_unknown_parent_is_not_given_the_standard_tier(self):
        for args in ({"agent_type": "leo-parent", "message": "x"}, {"message": "x"}):
            with self.subTest(args=args):
                result = engine.route("codex", "spawn_agent", args, None, config={}, catalog=self.catalog)
                self.assertEqual((result["action"], result["reason"]), ("allow", "parent-model-unavailable"))

    def test_parent_level_sends_no_model_so_claude_inherits(self):
        for parent in ("claude-opus-5-5", "us.anthropic.claude-opus-4-1-20250805-v1:0",
                       "claude-opus-4-1@20250805", "claude-mythos-preview"):
            with self.subTest(parent=parent):
                result = self.route({"subagent_type": "leos-agent:leo-parent"}, parent)
                self.assertEqual((result["action"], result["reason"]), ("allow", "inherits-parent"))
                self.assertIsNone(result["updated_input"])
                self.assertEqual(result["effective_model"], parent)

    def test_a_parent_without_an_alias_is_inherited_rather_than_blocked(self):
        catalog = pricing.snapshot({"data": self.catalog["models"] + [
            {"id": "anthropic/claude-mythos-preview", "pricing": {"prompt": "0.000001", "completion": "0.000005"}}]})
        explicit = self.route({"subagent_type": "general-purpose", "model": "opus"}, "claude-mythos-preview", catalog=catalog)
        self.assertEqual((explicit["action"], explicit["reason"]), ("correct", "over-ceiling"))
        self.assertNotIn("model", explicit["updated_input"])
        default = self.route({"subagent_type": "general-purpose"}, "claude-mythos-preview", catalog=catalog)
        self.assertEqual((default["action"], default["reason"]), ("allow", "inherits-parent"))
        # A pinned agent would fall back to its own model, not the parent's.
        pinned = self.route({"subagent_type": "leos-agent:leo-premium"}, "claude-mythos-preview", catalog=catalog)
        self.assertEqual(pinned["action"], "block")
        self.assertIn("leo-parent", pinned["retry"])

    def test_a_fork_runs_on_the_parent_and_is_never_corrected(self):
        for args in ({"subagent_type": "fork"}, {"subagent_type": "fork", "model": "opus"}):
            with self.subTest(args=args):
                result = self.route(args, "claude-sonnet-5-5")
                self.assertEqual((result["action"], result["reason"]), ("allow", "fork-inherits-parent"))
                self.assertIsNone(result["updated_input"])
                self.assertEqual(result["effective_model"], "claude-sonnet-5-5")

    def test_a_configured_full_id_is_never_sent_to_agent(self):
        config = {"claude": {"cheap": {"model": "claude-haiku-4-5-20251001", "effort": None}}}
        result = self.route({"subagent_type": "leos-agent:leo-cheap"}, "claude-opus-5-5", config=config)
        self.assertNotEqual(result["action"], "block")
        self.assertIsNone(result["updated_input"])

    def test_premium_cannot_pass_a_parent_whose_long_prompt_tier_overlaps_it(self):
        for args in ({"subagent_type": "leos-agent:leo-premium"}, {"subagent_type": "general-purpose", "model": "opus"}):
            with self.subTest(args=args):
                result = self.route(args, "claude-sonnet-4-5-20250929")
                self.assertEqual((result["action"], result["reason"]), ("correct", "over-ceiling"))
                self.assertEqual(result["updated_input"]["model"], "sonnet")
