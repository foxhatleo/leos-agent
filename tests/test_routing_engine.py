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

    def test_unknown_explicit_model_is_allowed_unchanged(self):
        result = self.route("claude", {"model": "corporate-new-model"}, "haiku")
        self.assertEqual(result["action"], "allow")
        self.assertEqual(result["reason"], "price-unknown")
        self.assertIsNone(result["updated_input"])

    def test_codex_does_not_emit_unsupported_rewrite_response(self):
        result = self.route("codex", {"message": "Investigate"}, "gpt-5.6-sol")
        self.assertEqual(result["action"], "block")
        self.assertIn("gpt-5.6-sol", result["retry"])
        self.assertIsNone(result["updated_input"])

    def test_native_codex_profile_precedence_is_respected(self):
        result = self.route("codex", {"model": "gpt-5.6-luna"}, "gpt-5.6-sol", effective_model="gpt-5.6-terra")
        self.assertEqual(result["reason"], "profile-over-ceiling")

    def test_codex_cheap_profile_cannot_silently_use_expensive_parent(self):
        result = self.route("codex", {"agent_type": "leo-cheap", "model": "gpt-6-astra"}, "gpt-6-astra")
        self.assertEqual(result["action"], "block")
        self.assertIn("gpt-5.6-luna", result["retry"])

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

    def test_only_owned_profiles_are_recognized(self):
        self.assertEqual(engine.tier_for("leos-agent:leo-runner"), "cheap")
        for name in ("leo-made-up", "other:leo-cheap", "path/leo-standard"):
            self.assertIsNone(engine.tier_for(name))
