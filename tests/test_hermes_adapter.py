import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


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

    def rows(self):
        import json
        path = Path(self.tmp.name) / "dispatch.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    # The host's own call shapes (tools/delegate_tool_results.py, model_tools.py):
    # subagent_stop fires once per child with no tool call id, before the tool
    # returns; post_tool_call then receives the tool result as a JSON string --
    # a dispatch handle for a background delegation, {"results": [...]} for a
    # synchronous one.
    def stop(self, child, summary, status="completed"):
        self.adapter._on_subagent_stop(parent_session_id="S", parent_turn_id="turn", child_session_id=child,
                                       child_role=None, child_summary=summary, child_status=status,
                                       tool_call_history=[], duration_ms=10)

    def post(self, result):
        self.adapter._on_post_tool_call(tool_name="delegate_task", args={"goal": "g"}, result=result, task_id="",
                                        session_id="S", tool_call_id="call-1", turn_id="", api_request_id="",
                                        duration_ms=5, status="ok", error_type=None, error_message=None, middleware_trace=[])

    def test_one_synchronous_delegation_is_one_child_each_not_two(self):
        self.stop("child-a", "Did it.\nResult: done\nVerified: PRIVATE ran pytest")
        self.post(json.dumps({"results": [{"task_index": 0, "status": "completed",
                                           "summary": "Did it.\nResult: done\nVerified: PRIVATE ran pytest"}]}))
        rows = self.rows()
        self.assertEqual([(r["outcome"], r["verified"], r["reason"], r["agent_id"]) for r in rows],
                         [("done", True, "hermes-subagent-stop", "child-a")])
        self.assertNotIn("PRIVATE", (Path(self.tmp.name) / "dispatch.jsonl").read_text())

    def test_a_background_handle_records_nothing_and_the_children_report_later(self):
        self.post(json.dumps({"status": "dispatched", "mode": "background", "count": 3, "delegation_id": "d1",
                              "goals": ["a", "b", "c"], "note": "Result: done is not a child outcome"}))
        self.assertEqual(self.rows(), [])
        for child, outcome in (("c1", "done"), ("c2", "blocked"), ("c3", "partial")):
            self.stop(child, "x\nResult: %s\nVerified: none" % outcome)
        import dispatch_log
        rows = dispatch_log.read()
        self.assertEqual([r["agent_id"] for r in rows], ["c1", "c2", "c3"])
        # Three children of one agent within seconds are three outcomes, not one.
        self.assertEqual(dispatch_log.summarise(rows)["outcomes"], {"unrouted": {"done": 1, "blocked": 1, "partial": 1}})

    def test_a_build_without_subagent_stop_reads_the_synchronous_json_result(self):
        # The JSON string escapes the summary's newlines; reading it as text
        # left `Result: done` mid-line and every outcome unknown.
        self.post(json.dumps({"results": [
            {"task_index": 0, "status": "completed", "summary": "Fixed.\nResult: done\nVerified: pytest green"},
            {"task_index": 1, "status": "failed", "summary": "Stuck.\nResult: blocked\nVerified: none"}]}))
        rows = self.rows()
        self.assertEqual([(r["outcome"], r["verified"], r["call_id"], r["status"]) for r in rows],
                         [("done", True, "call-1", "completed"), ("blocked", False, "call-1", "failed")])
        self.assertEqual(len({r["agent_id"] for r in rows}), 2)

    def test_post_tool_call_for_other_tools_launches_nothing(self):
        with patch.object(self.adapter, "_python") as run:
            self.assertIsNone(self.adapter._on_post_tool_call("terminal", {}, result="x"))
        run.assert_not_called()

    def test_registration_survives_a_build_without_completion_hooks(self):
        ctx = Mock()
        def refuse(name, callback):
            if name in ("post_tool_call", "subagent_stop"):
                raise TypeError("unknown hook")
        ctx.register_hook.side_effect = refuse
        self.adapter.register(ctx)
        self.assertIn("pre_tool_call", [c.args[0] for c in ctx.register_hook.call_args_list])

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
