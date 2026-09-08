import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class HermesAdapter(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("hermes_adapter_test", ROOT / "__init__.py")
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": self.tmp.name, "LEOS_AGENT_PRICE_REFRESH": "off", "LEOS_AGENT_DISPATCH_GUARD": "on"})
        env.start(); self.addCleanup(env.stop)

    def test_global_expensive_child_is_blocked_after_parent_observation(self):
        self.adapter._on_request_model(model="claude-haiku-4-5", task_id="t", conversation_history=["PRIVATE"])
        with patch.object(self.adapter, "_native_delegation_model", return_value="claude-opus-5"):
            result = self.adapter._on_pre_tool_call("delegate_task", {"tasks": [{"goal": "inspect"}]}, task_id="t")
        self.assertEqual(result["action"], "block")
        self.assertNotIn("PRIVATE", repr(self.adapter._PARENTS))

    def test_unrelated_tool_does_not_launch_process(self):
        with patch.object(self.adapter, "_python") as run:
            self.assertIsNone(self.adapter._on_pre_tool_call("terminal", {}))
        run.assert_not_called()

    def test_registration_uses_native_skill_signature_and_one_section(self):
        ctx = Mock()
        self.adapter.register(ctx)
        self.assertEqual(ctx.register_skill.call_count, len(list((ROOT / "skills").glob("*/SKILL.md"))))
        for call in ctx.register_skill.call_args_list:
            self.assertIsInstance(call.args[0], str)
            self.assertTrue(call.args[1].is_file())
        ctx.register_system_prompt_section.assert_called_once()
        text = self.adapter._payload_section({})
        self.assertIn("Cheap:", text)
        self.assertLess(len(text), 4000)
