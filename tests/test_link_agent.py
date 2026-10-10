"""Claude's PostToolUse on Agent links a dispatch's call id to its child, so joins are exact."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dispatch_guard
import dispatch_log
import link_agent
import observe_agent


def post_tool_use(call, child, status="completed", tool="Agent", session="parent-session"):
    """PostToolUse as Claude Code sends it after an Agent call (docs: PostToolUse
    input; Agent tool_response fields). A foreground run returns the child's
    final text and usage; a background launch returns at once with its agentId."""
    if status == "async_launched":
        response = {"isAsync": True, "status": status, "agentId": child, "description": "PRIVATE_DESCRIPTION",
                    "resolvedModel": "haiku", "prompt": "PRIVATE_PROMPT", "outputFile": "/tmp/PRIVATE_OUTPUT",
                    "canReadOutputFile": True, "canContinueAgent": True}
    else:
        response = {"status": status, "agentId": child, "prompt": "PRIVATE_PROMPT", "resolvedModel": "haiku",
                    "content": [{"type": "text", "text": "PRIVATE_RESULT\nResult: done\nVerified: pytest"}],
                    "totalTokens": 120, "totalDurationMs": 900, "totalToolUseCount": 3,
                    "usage": {"input_tokens": 100, "output_tokens": 20}}
    return {"session_id": session, "transcript_path": "/p/parent.jsonl", "cwd": "/w", "permission_mode": "default",
            "hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": {"subagent_type": "leos-agent:leo-cheap", "prompt": "PRIVATE_PROMPT", "description": "d"},
            "tool_response": response, "tool_use_id": call, "duration_ms": 900}


class Sandboxed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.root), "LEOS_AGENT_PRICE_REFRESH": "off",
                                           "LEOS_AGENT_DISPATCH_GUARD": "on", "LEOS_AGENT_HARNESS": "claude"})
        env.start(); self.addCleanup(env.stop)

    def raw(self):
        path = self.root / "dispatch.jsonl"
        return path.read_text() if path.exists() else ""


class LinkRows(Sandboxed):
    def test_foreground_and_background_results_record_ids_only(self):
        for call, child, status in (("toolu_1", "a1", "completed"), ("toolu_2", "a2", "async_launched")):
            dispatch_log.append(link_agent.link(post_tool_use(call, child, status)))
        rows = dispatch_log.read()
        self.assertEqual([(r["decision"], r["call_id"], r["agent_id"], r["status"]) for r in rows],
                         [("linked", "toolu_1", "a1", "completed"), ("linked", "toolu_2", "a2", "async_launched")])
        self.assertEqual(rows[0]["session"], dispatch_log.digest("parent-session"))
        self.assertNotIn("PRIVATE", self.raw())
        self.assertNotIn("haiku", self.raw())

    def test_events_that_name_no_child_write_nothing(self):
        broken = post_tool_use("toolu_3", "a3")
        del broken["tool_response"]["agentId"]
        for event in (post_tool_use("toolu_4", "a4", tool="Bash"), broken,
                      {**post_tool_use("toolu_5", "a5"), "hook_event_name": "PreToolUse"},
                      {**post_tool_use("toolu_6", "a6"), "tool_response": "a string result"}):
            self.assertIsNone(link_agent.link(event))

    def hermetic_env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LEOS_AGENT_")}
        env.update({"HOME": str(self.root), "LEOS_AGENT_LOCAL_PATH": str(self.root), "LEOS_AGENT_HARNESS": "claude",
                    "CLAUDE_CONFIG_DIR": str(self.root / ".claude"), "CODEX_HOME": str(self.root / ".codex"),
                    "HERMES_HOME": str(self.root / ".hermes"), "PI_CODING_AGENT_DIR": str(self.root / ".pi"),
                    "OPENCODE_CONFIG_DIR": str(self.root / ".opencode"), "OPENCODE_CONFIG": str(self.root / "oc.json"),
                    "XDG_CONFIG_HOME": str(self.root / ".config"), "PYTHONDONTWRITEBYTECODE": "1"})
        return env

    def test_the_real_hook_is_silent_and_writes_one_row(self):
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "link_agent.py")],
                              input=json.dumps(post_tool_use("toolu_7", "a7", "async_launched")),
                              capture_output=True, text=True, env=self.hermetic_env(), timeout=10)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertEqual([(r["call_id"], r["agent_id"]) for r in dispatch_log.read()], [("toolu_7", "a7")])

    def test_the_fast_path_loads_no_transcript_reader_or_catalog(self):
        script = ("import sys, json, io; sys.path.insert(0, %r); import link_agent; "
                  "sys.stdin = io.TextIOWrapper(io.BytesIO(json.dumps(%r).encode())); link_agent.main(); "
                  "print(json.dumps(sorted(m for m in ('pricing', 'routing_engine', 'session_models', 'observe_agent', "
                  "'outcome', 'dispatch_guard') if m in sys.modules)))") % (str(ROOT / "scripts"), post_tool_use("toolu_8", "a8"))
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              env=self.hermetic_env(), timeout=10)
        self.assertEqual(json.loads(done.stdout), [], done.stderr)


def dispatch(ts, call, agent="leos-agent:leo-cheap", session="s"):
    return {"v": 3, "ts": ts, "harness": "claude", "session": session, "decision": "correct", "tool": "Agent",
            "agent": agent, "tier": dispatch_log._tier_of({"agent": agent}), "call_id": call, "escalation_from": None}


def link(ts, call, child, session="s"):
    return {"v": 3, "ts": ts, "harness": "claude", "session": session, "decision": "linked", "call_id": call,
            "agent_id": child, "status": "async_launched"}


def stop(ts, child, outcome, agent="leos-agent:leo-cheap", session="s"):
    return {"v": 3, "ts": ts, "harness": "claude", "session": session, "decision": "executed", "agent": agent,
            "agent_id": child, "call_id": None, "outcome": outcome, "verified": True, "outcome_source": "message"}


class ExactJoins(unittest.TestCase):
    def joined(self, rows):
        completions, pairs, stats = dispatch_log.join(rows)
        return {row["agent_id"]: (pairs[id(row)][0] or {}).get("call_id") for row in completions}, stats

    def test_parallel_same_tier_children_join_their_own_calls(self):
        # Two cheap workers launched together; the first-launched one finishes first.
        rows = [dispatch("2026-10-01T10:00:00Z", "toolu_A"), dispatch("2026-10-01T10:00:01Z", "toolu_B"),
                stop("2026-10-01T10:00:30Z", "child-a", "done"), stop("2026-10-01T10:00:50Z", "child-b", "blocked")]
        guessed, stats = self.joined(rows)
        self.assertEqual(guessed, {"child-a": "toolu_B", "child-b": "toolu_A"})  # the nearest-preceding guess
        self.assertEqual(stats, {"nearest": 2})
        exact, stats = self.joined(rows + [link("2026-10-01T10:00:00Z", "toolu_A", "child-a"),
                                           link("2026-10-01T10:00:01Z", "toolu_B", "child-b")])
        self.assertEqual(exact, {"child-a": "toolu_A", "child-b": "toolu_B"})
        self.assertEqual(stats, {"linked": 2})

    def test_a_linked_dispatch_is_never_guessed_for_another_child(self):
        # toolu_B launched a background child that never sent SubagentStop; the
        # unlinked child must take toolu_A, not the nearer toolu_B.
        rows = [dispatch("2026-10-01T10:00:00Z", "toolu_A"), dispatch("2026-10-01T10:00:01Z", "toolu_B"),
                link("2026-10-01T10:00:01Z", "toolu_B", "child-b"), stop("2026-10-01T10:00:30Z", "child-a", "done")]
        joined, stats = self.joined(rows)
        self.assertEqual((joined, stats), ({"child-a": "toolu_A"}, {"nearest": 1}))
        summary = dispatch_log.summarise(rows)
        self.assertEqual(summary["coverage"], {"cheap": {"ran": 2, "no_signal": 1}})

    def test_a_link_whose_dispatch_rotated_away_does_not_guess(self):
        rows = [dispatch("2026-10-01T10:00:00Z", "toolu_A"), link("2026-10-01T09:59:00Z", "toolu_gone", "child-x"),
                stop("2026-10-01T10:00:30Z", "child-x", "done")]
        joined, stats = self.joined(rows)
        self.assertEqual((joined, stats), ({"child-x": None}, {"unmatched": 1}))

    def test_a_prefixed_child_id_matches_its_link(self):
        rows = [dispatch("2026-10-01T10:00:00Z", "toolu_A"), link("2026-10-01T10:00:00Z", "toolu_A", "a9f"),
                stop("2026-10-01T10:00:30Z", "agent-a9f", "done")]
        self.assertEqual(self.joined(rows), ({"agent-a9f": "toolu_A"}, {"linked": 1}))

    def test_link_rows_are_neither_dispatches_nor_completions(self):
        rows = [dispatch("2026-10-01T10:00:00Z", "toolu_A"), link("2026-10-01T10:00:00Z", "toolu_A", "a1"),
                stop("2026-10-01T10:00:30Z", "a1", "done")]
        summary = dispatch_log.summarise(rows)
        self.assertEqual((summary["records"], summary["links"], summary["dispatch_attempts"]), (3, 1, 1))
        self.assertEqual(summary["tiers"], {"leos-agent:leo-cheap": 1})
        self.assertEqual(summary["decisions"], {"correct": 1, "executed": 1})
        self.assertEqual(summary["reasons"], {"correct": {"none": 1}})
        self.assertIn("joins       linked 1", dispatch_log.render(summary))


class EndToEnd(Sandboxed):
    def test_guard_link_and_stop_join_exactly_through_the_real_paths(self):
        # PreToolUse for two parallel cheap workers, their PostToolUse results,
        # then SubagentStop with the first-launched child finishing first.
        for call in ("toolu_A", "toolu_B"):
            dispatch_guard.process({"hook_event_name": "PreToolUse", "session_id": "parent-session", "tool_use_id": call,
                                    "tool_name": "Agent", "tool_input": {"subagent_type": "leos-agent:leo-cheap",
                                                                         "prompt": "Inspect " + call, "model": "haiku"}}, "claude")
        for call, child in (("toolu_A", "child-a"), ("toolu_B", "child-b")):
            dispatch_log.append(link_agent.link(post_tool_use(call, child, "async_launched")))
        for child, line in (("child-a", "Result: done\nVerified: pytest"), ("child-b", "Result: blocked\nVerified: none")):
            observe_agent.observe({"hook_event_name": "SubagentStop", "session_id": "parent-session", "agent_id": child,
                                   "agent_type": "leos-agent:leo-cheap", "stop_hook_active": True,
                                   "last_assistant_message": line}, "claude")
        rows = dispatch_log.read()
        completions, pairs, stats = dispatch_log.join(rows)
        self.assertEqual(stats, {"linked": 2})
        self.assertEqual({row["agent_id"]: pairs[id(row)][0]["call_id"] for row in completions},
                         {"child-a": "toolu_A", "child-b": "toolu_B"})
        self.assertNotIn("PRIVATE", self.raw())


if __name__ == "__main__":
    unittest.main()
