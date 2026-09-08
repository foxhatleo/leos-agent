"""Native Cursor envelopes, including resolved child models and no allow override."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class CursorHooks(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("cursor_hook_test", ROOT / "scripts/cursor_hook.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": self.tmp.name, "LEOS_AGENT_PRICE_REFRESH": "off", "LEOS_AGENT_DISPATCH_GUARD": "on"})
        env.start(); self.addCleanup(env.stop)

    def event(self, child, parent):
        return {"hook_event_name": "subagentStart", "subagent_type": "generalPurpose", "task": "Inspect a change",
                "subagent_model": child, "model_id": parent, "parent_conversation_id": "c", "tool_call_id": "t"}

    def test_resolved_expensive_child_is_denied(self):
        result = self.module.handle(self.event("claude-opus-5", "claude-haiku-4-5"))
        self.assertEqual(result["permission"], "deny")
        self.assertIn("parent", result["user_message"])

    def test_cheap_and_unknown_children_do_not_override_permissions(self):
        self.assertIsNone(self.module.handle(self.event("claude-haiku-4-5", "claude-opus-5")))
        self.assertIsNone(self.module.handle(self.event("custom-model", "claude-opus-5")))

    def test_lifecycle_never_injects_policy_or_claims_executed_model(self):
        self.assertIsNone(self.module.handle({"hook_event_name": "sessionStart", "conversation_id": "c", "model_id": "claude-opus-5"}))
        self.assertIsNone(self.module.handle({"hook_event_name": "subagentStop", "conversation_id": "c", "status": "completed"}))
        rows = self.module.dispatch_log.read()
        self.assertEqual(rows[-1]["decision"], "completed")
        self.assertIsNone(rows[-1]["effective_model"])

    def test_unknown_resolved_model_does_not_use_config_as_observation(self):
        event = self.event(None, "claude-haiku-4-5")
        event["subagent_type"] = "leo-standard"
        self.assertIsNone(self.module.handle(event))
        self.assertEqual(self.module.dispatch_log.read()[-1]["reason"], "per-dispatch-routing-unavailable")
