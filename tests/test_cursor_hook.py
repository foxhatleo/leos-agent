"""Native Cursor envelopes, including resolved child models and a plain explicit allow."""
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

    def test_an_expensive_lens_is_told_to_review_the_area_itself_not_to_use_leo_parent(self):
        # Cursor's leo-parent has no readonly flag, so the lens must not be sent there.
        event = dict(self.event("claude-opus-5", "claude-haiku-4-5"), subagent_type="leo-lens")
        result = self.module.handle(event)
        self.assertEqual(result["permission"], "deny")
        self.assertIn("review that area yourself", result["agent_message"].lower())
        self.assertNotIn("parent-level", result["agent_message"])
        worker = self.module.handle(dict(event, subagent_type="leo-standard"))
        self.assertEqual(worker["permission"], "deny")
        self.assertNotIn("review that area yourself", worker["agent_message"].lower())

    def test_cheap_and_unknown_children_get_a_plain_allow(self):
        # Cursor blocks a permission hook that gives no valid response, so an
        # allowed start answers allow and adds nothing else.
        self.assertEqual(self.module.handle(self.event("claude-haiku-4-5", "claude-opus-5")), {"permission": "allow"})
        self.assertEqual(self.module.handle(self.event("custom-model", "claude-opus-5")), {"permission": "allow"})

    def test_lifecycle_never_injects_policy_or_claims_executed_model(self):
        self.assertIsNone(self.module.handle({"hook_event_name": "sessionStart", "conversation_id": "c", "model_id": "claude-opus-5"}))
        self.assertIsNone(self.module.handle({"hook_event_name": "subagentStop", "conversation_id": "c", "status": "completed"}))
        rows = self.module.dispatch_log.read()
        self.assertEqual(rows[-1]["decision"], "completed")
        self.assertIsNone(rows[-1]["effective_model"])
        # A finished status is not a done outcome: Cursor never shows child text.
        self.assertEqual((rows[-1]["outcome"], rows[-1]["outcome_source"], rows[-1]["status"]), ("unknown", "status-only", "completed"))
        self.assertIsNone(rows[-1]["verified"])

    def test_parallel_children_join_by_call_id_and_carry_their_summary(self):
        # subagentStop as current Cursor sends it: the Task call id, the child
        # conversation id, a status token, and the child's summary.
        for call in ("t1", "t2"):
            event = self.event("claude-haiku-4-5", "claude-opus-5")
            event.update(subagent_type="leo-cheap", tool_call_id=call)
            self.module.handle(event)
        for call, result in (("t2", "blocked"), ("t1", "done")):
            self.module.handle({"hook_event_name": "subagentStop", "subagent_type": "leo-cheap", "parent_conversation_id": "c",
                                "tool_call_id": call, "child_conversation_id": "child-" + call, "status": "completed",
                                "description": "Inspect", "summary": "PRIVATE_SUMMARY\nResult: %s\nVerified: none" % result,
                                "modified_files": [], "agent_transcript_path": None})
        rows = self.module.dispatch_log.read()
        stops = [r for r in rows if r["decision"] == "completed"]
        self.assertEqual([(r["call_id"], r["agent_id"], r["outcome"], r["outcome_source"]) for r in stops],
                         [("t2", "child-t2", "blocked", "message"), ("t1", "child-t1", "done", "message")])
        summary = self.module.dispatch_log.summarise(rows)
        self.assertEqual(summary["joins"], {"call_id": 2})
        self.assertEqual(summary["outcomes"], {"cheap": {"blocked": 1, "done": 1}})
        self.assertNotIn("PRIVATE_SUMMARY", (Path(self.tmp.name) / "dispatch.jsonl").read_text())

    def test_unknown_resolved_model_does_not_use_config_as_observation(self):
        event = self.event(None, "claude-haiku-4-5")
        event["subagent_type"] = "leo-standard"
        self.assertEqual(self.module.handle(event), {"permission": "allow"})
        self.assertEqual(self.module.dispatch_log.read()[-1]["reason"], "per-dispatch-routing-unavailable")
