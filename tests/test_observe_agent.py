"""Completion signals: fixed tokens and counts from a child's final text, never the text."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dispatch_log
import observe_agent
import session_models

REPLY = "Changed scripts/x.py.\n\nResult: done\nVerified: ran unittest, 9 tests PRIVATE_EVIDENCE"


class Signals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.root)})
        env.start(); self.addCleanup(env.stop)

    def claude_transcript(self, name, text=REPLY, usage=None, model="haiku"):
        path = self.root / (name + ".jsonl")
        rows = [{"type": "user", "message": {"content": "PRIVATE_BRIEF"}},
                {"type": "assistant", "message": {"id": "m1", "model": model, "usage": usage or {"input_tokens": 10, "output_tokens": 2},
                                                  "content": [{"type": "tool_use", "name": "Read"}]}},
                {"type": "assistant", "message": {"id": "m1", "model": model, "usage": usage or {"input_tokens": 10, "output_tokens": 2},
                                                  "content": [{"type": "text", "text": "thinking aloud"}]}},
                {"type": "assistant", "message": {"id": "m2", "model": model, "usage": {"input_tokens": 5, "output_tokens": 3, "cache_read_input_tokens": 40},
                                                  "content": [{"type": "text", "text": text}]}}]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return str(path)

    def raw(self):
        return (self.root / "dispatch.jsonl").read_bytes()

    def test_native_message_is_classified_and_dropped(self):
        observe_agent.observe({"hook_event_name": "SubagentStop", "agent_type": "leo-cheap", "agent_id": "a",
                               "session_id": "s", "tool_use_id": "t", "last_assistant_message": REPLY}, "claude")
        row = dispatch_log.read()[-1]
        self.assertEqual((row["outcome"], row["verified"], row["outcome_source"]), ("done", True, "message"))
        self.assertIsNone(row["usage"])
        self.assertNotIn(b"PRIVATE_EVIDENCE", self.raw())
        self.assertNotIn(b"Changed scripts", self.raw())

    def test_transcript_fallback_supplies_text_and_usage(self):
        path = self.claude_transcript("kid", text="Blocked on permissions.\nResult: blocked\nVerified: none")
        observe_agent.observe({"hook_event_name": "SubagentStop", "agent_id": "kid", "agent_transcript_path": path}, "claude")
        row = dispatch_log.read()[-1]
        self.assertEqual((row["outcome"], row["verified"], row["outcome_source"], row["decision"]),
                         ("blocked", False, "transcript", "executed"))
        # m1 repeated across two records counts once; m2 adds its own.
        self.assertEqual(row["usage"], {"input": 15, "output": 5, "cache_read": 40})
        self.assertIs(row["usage_complete"], True)
        self.assertNotIn(b"PRIVATE_BRIEF", self.raw())

    def test_no_text_anywhere_is_unknown_not_invented(self):
        observe_agent.observe({"hook_event_name": "SubagentStop", "agent_id": "ghost", "session_id": "s"}, "claude")
        row = dispatch_log.read()[-1]
        self.assertEqual((row["outcome"], row["verified"], row["outcome_source"], row["usage"]), ("unknown", None, None, None))

    def test_a_status_only_event_says_so(self):
        observe_agent.observe({"hook_event_name": "SubagentStop", "session_id": "s", "status": "completed"}, "cursor")
        row = dispatch_log.read()[-1]
        self.assertEqual((row["outcome"], row["outcome_source"], row["status"]), ("unknown", "status-only", "completed"))

    def test_only_the_tail_of_a_huge_message_is_parsed(self):
        huge = ("x" * 200_000) + "\nResult: partial\nVerified: n/a"
        observe_agent.observe({"hook_event_name": "SubagentStop", "agent_id": "big", "last_assistant_message": huge}, "codex")
        row = dispatch_log.read()[-1]
        self.assertEqual((row["outcome"], row["verified"]), ("partial", False))
        self.assertLess(len(self.raw()), 2000)

    def test_declared_source_and_usage_from_the_envelope_win(self):
        observe_agent.observe({"hook_event_name": "SubagentStop", "toolCallId": "c1", "result_text": "Result: escalate\nVerified: none",
                               "outcome_source": "tool-output", "usage": {"input": 7, "output": 1}}, "opencode")
        row = dispatch_log.read()[-1]
        self.assertEqual((row["outcome"], row["outcome_source"], row["usage"], row["call_id"]),
                         ("escalate", "tool-output", {"input": 7, "output": 1}, "c1"))

    def test_session_end_backfills_a_child_that_flushed_late(self):
        parent = self.root / "session.jsonl"
        child = self.root / "session/subagents/agent-late.jsonl"
        observe_agent.observe({"hook_event_name": "SubagentStop", "session_id": "s", "agent_id": "late",
                               "agent_transcript_path": str(child)}, "claude")
        self.assertEqual(dispatch_log.read()[-1]["outcome"], "unknown")
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({"type": "assistant", "message": {"id": "m", "model": "haiku", "usage": {"input_tokens": 1, "output_tokens": 1},
                                                                       "content": [{"type": "text", "text": "Result: done\nVerified: diff"}]}}) + "\n")
        observe_agent.observe({"hook_event_name": "SessionEnd", "session_id": "s", "transcript_path": str(parent)}, "claude")
        rows = [r for r in dispatch_log.read() if r["decision"] == "executed"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["outcome"], rows[0]["verified"], rows[0]["usage"]), ("done", True, {"input": 1, "output": 1}))

    def test_codex_rollout_shapes_are_read(self):
        path = self.root / "rollout.jsonl"
        rows = [{"type": "turn_context", "payload": {"model": "gpt-5.6-luna"}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                      "content": [{"type": "output_text", "text": "Result: done\nVerified: pytest"}]}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 9}}}}]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        self.assertEqual(session_models.transcript_tail_text(str(path)), "Result: done\nVerified: pytest")
        self.assertEqual(session_models.transcript_usage(str(path)), ({"input": 100, "output": 9, "cache_read": 60}, True))

    def test_an_oversized_transcript_reports_incomplete_usage(self):
        path = self.claude_transcript("fat")
        with patch.object(session_models.os.path, "getsize", return_value=session_models.USAGE_READ_LIMIT + 1):
            self.assertEqual(session_models.transcript_usage(path), (None, False))


if __name__ == "__main__":
    unittest.main()
