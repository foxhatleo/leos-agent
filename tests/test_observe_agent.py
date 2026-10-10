"""Completion signals: fixed tokens and counts from a child's final text, never the text."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dispatch_log
import observe_agent
import outcome
import session_models

REPLY = "Changed scripts/x.py.\n\nResult: done\nVerified: ran unittest, 9 tests PRIVATE_EVIDENCE"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "observe_agent.py"


def hermetic_env(root):
    """The hook's environment with HOME and every harness config override inside `root`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEOS_AGENT_")}
    env.update({"HOME": str(root), "LEOS_AGENT_LOCAL_PATH": str(root), "LEOS_AGENT_PRICE_REFRESH": "off",
                "CLAUDE_CONFIG_DIR": str(root / ".claude"), "CODEX_HOME": str(root / ".codex"),
                "HERMES_HOME": str(root / ".hermes"), "PI_CODING_AGENT_DIR": str(root / ".pi"),
                "OPENCODE_CONFIG_DIR": str(root / ".opencode"), "OPENCODE_CONFIG": str(root / "opencode.json"),
                "XDG_CONFIG_HOME": str(root / ".config"), "PYTHONDONTWRITEBYTECODE": "1"})
    return env


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

    def test_claude_internal_agents_are_not_completions(self):
        # docs: SubagentStop also fires for Claude's own internal agents, with
        # agent_type "" when the session runs without --agent.
        observe_agent.observe({"hook_event_name": "SubagentStop", "agent_type": "", "agent_id": "suggest",
                               "session_id": "s", "stop_hook_active": False, "last_assistant_message": "Try: run tests"}, "claude")
        with self.assertRaises(FileNotFoundError):
            self.raw()

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
        observe_agent.reconcile("s", str(parent))
        rows = [r for r in dispatch_log.read() if r["decision"] == "executed"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["outcome"], rows[0]["verified"], rows[0]["usage"], rows[0]["turns"]),
                         ("done", True, {"input": 1, "output": 1}, 1))

    def test_codex_rollout_shapes_are_read(self):
        path = self.root / "rollout.jsonl"
        rows = [{"type": "turn_context", "payload": {"model": "gpt-5.6-luna"}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                      "content": [{"type": "output_text", "text": "Result: done\nVerified: pytest"}]}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 9},
                    "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 9}}}}]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        self.assertEqual(session_models.transcript_tail_text(str(path)), "Result: done\nVerified: pytest")
        # Codex input includes its cached input; the cached part is counted once, as cache.
        self.assertEqual(session_models.transcript_stats(str(path)),
                         {"usage": {"input": 40, "output": 9, "cache_read": 60}, "complete": True, "turns": 1})

    def test_streaming_snapshots_of_one_request_keep_their_largest_counts(self):
        # Claude writes one request's usage as snapshots under one requestId;
        # the first snapshot's output_tokens is 1, the final one's 850.
        path = self.root / "stream.jsonl"
        rows = [{"type": "assistant", "requestId": "r1", "message": {"id": "m1", "model": "haiku", "content": [],
                                                                     "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 500}}},
                {"type": "assistant", "requestId": "r1", "message": {"id": "m1", "model": "haiku", "content": [],
                                                                     "usage": {"input_tokens": 10, "output_tokens": 850, "cache_read_input_tokens": 500}}},
                {"type": "assistant", "requestId": "r2", "message": {"id": "m2", "model": "haiku", "content": [],
                                                                     "usage": {"input_tokens": 3, "output_tokens": 7}}}]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        self.assertEqual(session_models.transcript_stats(str(path)),
                         {"usage": {"input": 13, "output": 857, "cache_read": 500}, "complete": True, "turns": 2})

    def test_child_usage_agrees_with_the_usage_scan(self):
        import usage_scan
        stamp = "2026-10-01T10:00:00Z"
        claude = self.root / "claude" / "proj" / "sess" / "subagents" / "agent-k.jsonl"
        claude.parent.mkdir(parents=True)
        claude.write_text("\n".join(json.dumps(r) for r in [
            {"type": "assistant", "timestamp": stamp, "requestId": "r1", "message": {"id": "m1", "model": "haiku", "content": [],
                                                                                     "usage": {"input_tokens": 4, "output_tokens": 1, "cache_creation_input_tokens": 90}}},
            {"type": "assistant", "timestamp": stamp, "requestId": "r1", "message": {"id": "m1", "model": "haiku", "content": [],
                                                                                     "usage": {"input_tokens": 4, "output_tokens": 60, "cache_creation_input_tokens": 90}}},
        ]) + "\n")
        scanned = usage_scan.scan_claude(0, str(self.root / "claude"))["model_usage"]["haiku"]["subagent"]
        usage = session_models.transcript_stats(str(claude))["usage"]
        self.assertEqual({k: usage.get(k, 0) for k in ("input", "cache_read", "cache_write", "output")},
                         {k: scanned[k] for k in ("input", "cache_read", "cache_write", "output")})
        codex = self.root / "codex" / "sessions" / "rollout-kid.jsonl"
        codex.parent.mkdir(parents=True)
        totals = [(100, 60, 9), (250, 200, 30)]
        codex.write_text("\n".join(json.dumps(r) for r in [
            {"type": "session_meta", "timestamp": stamp, "payload": {"source": {"subagent": {}}}},
            {"type": "turn_context", "timestamp": stamp, "payload": {"model": "gpt-5.6-luna"}}] + [
            {"type": "event_msg", "timestamp": stamp, "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o},
                "last_token_usage": {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o}}}} for i, c, o in totals]) + "\n")
        scanned = usage_scan.scan_codex(0, str(self.root / "codex" / "sessions"))["model_usage"]["gpt-5.6-luna"]["subagent"]
        stats = session_models.transcript_stats(str(codex))
        self.assertEqual({k: stats["usage"].get(k, 0) for k in ("input", "cache_read", "cache_write", "output")},
                         {k: scanned[k] for k in ("input", "cache_read", "cache_write", "output")})
        self.assertEqual(stats["turns"], 2)

    def test_a_handed_back_report_supplies_the_outcome(self):
        # In auto mode a child reports through the hand-back tool; its last
        # message is only closing text (docs: SubagentStop input).
        path = self.root / "handback.jsonl"
        rows = [{"type": "assistant", "message": {"id": "m1", "model": "haiku", "usage": {"input_tokens": 3, "output_tokens": 4},
                                                  "content": [{"type": "tool_use", "name": "SubagentHandback",
                                                               "input": {"message": "PRIVATE_REPORT\nResult: partial\nVerified: ran make"}}]}},
                {"type": "assistant", "message": {"id": "m2", "model": "haiku", "usage": {"input_tokens": 1, "output_tokens": 1},
                                                  "content": [{"type": "text", "text": "Handed back."}]}}]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        observe_agent.observe({"hook_event_name": "SubagentStop", "agent_type": "leos-agent:leo-cheap", "agent_id": "h",
                               "session_id": "s", "stop_hook_active": False, "permission_mode": "auto",
                               "agent_transcript_path": str(path), "last_assistant_message": "Handed back."}, "claude")
        row = dispatch_log.read()[-1]
        self.assertEqual((row["outcome"], row["verified"], row["outcome_source"]), ("partial", True, "handback"))
        self.assertNotIn(b"PRIVATE_REPORT", self.raw())

    def test_session_end_backfills_an_outcome_that_flushed_late(self):
        parent = self.root / "late.jsonl"
        child = self.root / "late/subagents/agent-h2.jsonl"
        child.parent.mkdir(parents=True)
        rows = [{"type": "assistant", "message": {"id": "m1", "model": "haiku", "content": [{"type": "text", "text": "Done."}]}}]
        child.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        observe_agent.observe({"hook_event_name": "SubagentStop", "agent_type": "leo-cheap", "agent_id": "h2", "session_id": "s2",
                               "transcript_path": str(parent), "last_assistant_message": "Handed back."}, "claude")
        self.assertEqual((dispatch_log.read()[-1]["decision"], dispatch_log.read()[-1]["outcome"]), ("executed", "unknown"))
        rows.insert(0, {"type": "assistant", "message": {"id": "m0", "model": "haiku", "content": [
            {"type": "tool_use", "name": "SubagentHandback", "input": {"message": "Result: done\nVerified: pytest"}}]}})
        child.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        observe_agent.reconcile("s2", str(parent))
        observe_agent.reconcile("s2", str(parent))
        rows = dispatch_log.read()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[-1]["outcome"], rows[-1]["verified"], rows[-1]["outcome_source"], rows[-1]["reason"]),
                         ("done", True, "handback", "session-end-late-outcome"))
        self.assertEqual(dispatch_log.summarise(rows)["outcomes"], {"cheap": {"done": 1}})

    def test_the_session_end_hook_returns_at_once_and_reconciles_detached(self):
        """The real hook, as Claude Code runs it: SessionEnd hooks share a 1.5 s
        budget that a plugin's timeout cannot raise, so the hook only hands off."""
        parent = self.root / "sess.jsonl"
        child = self.root / "sess/subagents/agent-bg.jsonl"
        observe_agent.observe({"hook_event_name": "SubagentStop", "session_id": "s3", "agent_id": "bg",
                               "agent_transcript_path": str(child)}, "claude")
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({"type": "assistant", "message": {"id": "m", "model": "haiku",
                                                                       "content": [{"type": "text", "text": "Result: done\nVerified: diff"}]}}) + "\n")
        env = {**hermetic_env(self.root), "LEOS_AGENT_HARNESS": "claude"}
        started = time.monotonic()
        done = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps({
            "hook_event_name": "SessionEnd", "session_id": "s3", "transcript_path": str(parent), "reason": "other"}),
            capture_output=True, text=True, env=env, timeout=10)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "{}"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(r["decision"] == "executed" for r in dispatch_log.read()):
            time.sleep(0.05)
        rows = [r for r in dispatch_log.read() if r["decision"] == "executed"]
        self.assertEqual([(r["effective_model"], r["outcome"]) for r in rows], [("haiku", "done")])

    def test_session_end_reconciles_inline_when_it_cannot_detach(self):
        parent = self.root / "inline.jsonl"
        child = self.root / "inline/subagents/agent-il.jsonl"
        child.parent.mkdir(parents=True)
        observe_agent.observe({"hook_event_name": "SubagentStop", "session_id": "s4", "agent_id": "il",
                               "agent_transcript_path": str(child)}, "claude")
        child.write_text(json.dumps({"type": "assistant", "message": {"id": "m", "model": "haiku", "content": []}}) + "\n")
        with patch.object(observe_agent.subprocess, "Popen", side_effect=OSError("no fork")):
            observe_agent.observe({"hook_event_name": "SessionEnd", "session_id": "s4", "transcript_path": str(parent)}, "claude")
        self.assertEqual(dispatch_log.read()[-1]["effective_model"], "haiku")

    def test_an_oversized_transcript_reports_incomplete_usage(self):
        path = self.claude_transcript("fat")
        with patch.object(session_models.os.path, "getsize", return_value=session_models.USAGE_READ_LIMIT + 1):
            self.assertEqual(session_models.transcript_stats(path), {"usage": None, "complete": False, "turns": None})


class ContractPrompt(unittest.TestCase):
    """SubagentStop as Claude Code and Codex send it: stop_hook_active is false
    on a first stop and true once a hook has kept the child running."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.root), "LEOS_AGENT_DISPATCH_GUARD": "on"})
        env.start(); self.addCleanup(env.stop)

    def stop(self, text="I changed the parser and the tests pass.", agent="leos-agent:leo-cheap", active=False, **extra):
        event = {"hook_event_name": "SubagentStop", "session_id": "s", "agent_id": "c1", "agent_type": agent,
                 "permission_mode": "default", "stop_hook_active": active, "last_assistant_message": text,
                 "transcript_path": str(self.root / "parent.jsonl"),
                 "agent_transcript_path": str(self.root / "parent/subagents/agent-c1.jsonl")}
        event.update(extra)
        return event

    def run_hook(self, event, harness):
        env = {**hermetic_env(self.root), "LEOS_AGENT_HARNESS": harness, "LEOS_AGENT_DISPATCH_GUARD": "on"}
        done = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(event), capture_output=True,
                              text=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def rows(self):
        try:
            return dispatch_log.read()
        except OSError:
            return []

    def test_a_worker_missing_its_lines_is_asked_once_then_recorded(self):
        reply = self.run_hook(self.stop(), "claude")
        context = reply["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "SubagentStop")
        self.assertIn("Result:", context["additionalContext"])
        self.assertEqual(self.rows(), [])  # the child is still running
        self.assertEqual(self.run_hook(self.stop(active=True), "claude"), {})
        self.assertEqual([(r["outcome"], r["agent"]) for r in self.rows()], [("unknown", "leos-agent:leo-cheap")])

    def test_a_lens_report_is_held_to_the_worker_contract(self):
        report = json.dumps({"status": "done", "covered_paths": ["src/a.py"], "gaps": [], "findings": []})
        lens = "leos-agent:leo-lens"
        self.assertIsNotNone(observe_agent.observe(self.stop(text=report, agent=lens), "claude"))
        closed = report + "\nResult: done\nVerified: ghreview.py extract at the pinned SHA"
        self.assertIsNone(observe_agent.observe(self.stop(text=closed, agent=lens), "claude"))
        self.assertEqual([(r["agent"], r["outcome"]) for r in self.rows()], [(lens, "done")])

    def test_codex_gets_its_block_decision(self):
        reply = self.run_hook(self.stop(agent="leo-standard"), "codex")
        self.assertEqual(reply["decision"], "block")
        self.assertIn("Verified:", reply["reason"])

    def test_the_prompt_cannot_satisfy_itself_when_echoed(self):
        self.assertEqual(outcome.parse(observe_agent.CONTRACT_PROMPT), {"outcome": "unknown", "verified": None})

    def test_who_is_never_prompted(self):
        cases = [
            (self.stop(text="Done.\nResult: done\nVerified: ran pytest"), "claude"),
            (self.stop(agent="general-purpose"), "claude"),
            (self.stop(agent="other-plugin:leo-cheap"), "claude"),
            ({k: v for k, v in self.stop().items() if k != "stop_hook_active"}, "claude"),
            ({k: v for k, v in self.stop().items() if k != "last_assistant_message"}, "codex"),
            (self.stop(), "cursor"),
            (self.stop(), "opencode"),
        ]
        for event, harness in cases:
            self.assertIsNone(observe_agent.observe(event, harness), (event.get("agent_type"), harness))
        for mode in ("off", "warn"):
            with patch.dict(os.environ, {"LEOS_AGENT_DISPATCH_GUARD": mode}):
                self.assertIsNone(observe_agent.observe(self.stop(), "claude"), mode)

    def test_a_handed_back_report_is_never_prompted_again(self):
        child = self.root / "parent/subagents/agent-c1.jsonl"
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "SubagentHandback", "input": {"message": "report without lines"}}]}}) + "\n")
        self.assertIsNone(observe_agent.observe(self.stop(text="", permission_mode="auto"), "claude"))

    def test_auto_mode_waits_for_the_transcript_before_judging_a_missing_hand_back(self):
        # The transcript lags the stop event, so a hand-back may not be on disk yet.
        self.assertIsNone(observe_agent.observe(self.stop(permission_mode="auto"), "claude"))
        child = self.root / "parent/subagents/agent-c1.jsonl"
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I changed the parser and the tests pass."}]}}) + "\n")
        self.assertIsNotNone(observe_agent.contract_prompt(self.stop(permission_mode="auto"), "claude"))


if __name__ == "__main__":
    unittest.main()
