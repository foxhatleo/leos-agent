"""Parent observations are bounded, model-only, and honor native event precedence."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import session_models
import observe_agent
import dispatch_log


class ModelObservations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.root)})
        env.start(); self.addCleanup(env.stop)

    def transcript(self, model):
        path = self.root / (model + ".jsonl")
        path.write_text(json.dumps({"type": "assistant", "message": {"model": model, "content": "PRIVATE_PROMPT"}}) + "\n")
        return str(path)

    def test_active_codex_event_beats_stale_transcript(self):
        self.assertEqual(session_models.parent_model({"model": "gpt-5.6-luna", "transcript_path": self.transcript("gpt-6-astra")}, "codex"), "gpt-5.6-luna")

    def test_transcript_ignores_fragments_and_synthetic_models(self):
        path = self.root / "child.jsonl"
        path.write_text('x' * (1024 * 1024 + 100) + '\n' + json.dumps({"type": "assistant", "message": {"model": "haiku"}}) + '\n' + json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}))
        self.assertEqual(session_models.transcript_model(str(path)), "haiku")

    def test_session_observations_expire(self):
        with patch.object(session_models.time, "time", return_value=1):
            session_models.remember({"session_id": "s", "model": "haiku"}, "claude")
        with patch.object(session_models.time, "time", return_value=90000):
            self.assertIsNone(session_models.parent_model({"session_id": "s"}, "claude"))

    def test_nested_claude_uses_immediate_caller_not_root(self):
        root = self.transcript("opus")
        child = self.root / "opus" / "subagents" / "agent-child.jsonl"
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({"type": "assistant", "message": {"model": "haiku"}}))
        event = {"agent_id": "child", "transcript_path": root}
        self.assertEqual(session_models.parent_model(event, "claude"), "haiku")
        child.unlink()
        self.assertIsNone(session_models.parent_model(event, "claude"))

    def test_child_observer_never_uses_parent_model_or_content(self):
        observe_agent.observe({"hook_event_name": "SubagentStop", "model": "opus", "transcript_path": self.transcript("opus"), "agent_id": "a"}, "claude")
        self.assertIsNone(dispatch_log.read()[-1]["effective_model"])
        observe_agent.observe({"hook_event_name": "SubagentStop", "agent_transcript_path": self.transcript("haiku"), "agent_id": "b"}, "claude")
        rows = dispatch_log.read()
        self.assertEqual(rows[-1]["effective_model"], "haiku")
        self.assertEqual(rows[-1]["decision"], "executed")
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(rows))

    def test_lifecycle_reads_every_spelling_the_guard_does(self):
        """Claude spells the child's fields differently across hook versions.
        The guard already tolerates all of them; when the observer read one
        spelling, 70 of 84 rows in a day landed as `- @ unknown`."""
        observe_agent.observe({"hook_event_name": "SubagentStop", "subagentType": "leos-agent:leo-cheap",
                               "agentId": "c1", "sessionId": "s", "toolCallId": "t1",
                               "agentTranscriptPath": self.transcript("haiku")}, "claude")
        row = dispatch_log.read()[-1]
        self.assertEqual((row["agent"], row["agent_id"], row["call_id"], row["effective_model"], row["decision"]),
                         ("leos-agent:leo-cheap", "c1", "t1", "haiku", "executed"))
        self.assertEqual(row["session"], dispatch_log.digest("s"))

    def test_a_stop_without_a_child_path_derives_it_from_agent_id(self):
        root = self.transcript("opus")
        child = self.root / "opus" / "subagents" / "agent-kid.jsonl"
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({"type": "assistant", "message": {"model": "sonnet"}}))
        observe_agent.observe({"hook_event_name": "SubagentStop", "agent_type": "leo-standard",
                               "agent_id": "kid", "transcript_path": root}, "claude")
        row = dispatch_log.read()[-1]
        self.assertEqual((row["agent"], row["effective_model"], row["decision"]), ("leo-standard", "sonnet", "executed"))

    def test_child_transcript_refuses_unsafe_ids(self):
        self.assertIsNone(session_models.child_transcript({"agent_id": "../etc", "transcript_path": "/t.jsonl"}))
        self.assertIsNone(session_models.child_transcript({"agent_id": "ok"}))
        self.assertEqual(session_models.child_transcript({"agentId": "ok", "transcriptPath": "/t.jsonl"}),
                         str(Path("/t") / "subagents" / "agent-ok.jsonl"))
        self.assertEqual(session_models.child_transcript({"agentTranscriptPath": "/c.jsonl", "agent_id": "x"}), "/c.jsonl")

    def test_session_end_recovers_delayed_child_model_once(self):
        parent = self.root / "session.jsonl"
        child = self.root / "session/subagents/agent-delayed.jsonl"
        observe_agent.observe({"hook_event_name": "SubagentStop", "session_id": "s",
                               "agent_id": "delayed", "agent_transcript_path": str(child)}, "claude")
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({"type": "assistant", "message": {"model": "haiku"}}))
        event = {"hook_event_name": "SessionEnd", "session_id": "s", "transcript_path": str(parent)}
        observe_agent.observe(event, "claude")
        observe_agent.observe(event, "claude")
        rows = [r for r in dispatch_log.read() if r["decision"] == "executed"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["effective_model"], "haiku")
