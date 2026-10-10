"""A tier worker's hand-back report is refused once when it lacks its contract lines.

Claude Code as observed on 2.1.296, in auto mode: a subagent delivers its report
by calling SubagentHandback({message}). The call first runs the PreToolUse
command hooks whose matcher covers the tool, with the subagent's agent_id and
agent_type in the input and the parent's transcript as transcript_path. A hook
`deny` stops the call; its permissionDecisionReason comes back to the subagent
as the tool's error result, and the transcript records the call and that error.
Otherwise the report is delivered. These tests drive the hook exactly as
hooks.json wires it and apply its reply the same way.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_log  # noqa: E402
import handback_contract  # noqa: E402
import observe_agent  # noqa: E402
import outcome  # noqa: E402
from test_claude_guard import CLAUDE_TOOLS, claude_matches  # noqa: E402
from test_observe_agent import hermetic_env  # noqa: E402

BARE = "I fixed the parser; the suite passes."
CLOSED = BARE + "\n\nResult: done\nVerified: python3 -m unittest, 41 tests"


def handback_commands():
    groups = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    return [hook["command"] for group in groups if claude_matches(group["matcher"], "SubagentHandback")
            for hook in group["hooks"]]


class Host(unittest.TestCase):
    """One session's data directory, parent transcript and children."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "local"
        self.parent = self.root / "projects" / "p" / "session-1.jsonl"
        self.parent.parent.mkdir(parents=True)
        self.parent.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5"}}) + "\n")
        self.calls = 0
        sandbox = {k: v for k, v in hermetic_env(self.root).items() if k != "PYTHONDONTWRITEBYTECODE"}
        env = patch.dict(os.environ, {**sandbox, "LEOS_AGENT_LOCAL_PATH": str(self.data), "LEOS_AGENT_DISPATCH_GUARD": "on"})
        env.start(); self.addCleanup(env.stop)

    def child_transcript(self, child):
        return self.parent.with_suffix("") / "subagents" / ("agent-" + child + ".jsonl")

    def event(self, report, child="c1", agent="leos-agent:leo-cheap"):
        self.calls += 1
        event = {"session_id": "session-1", "prompt_id": "p1", "transcript_path": str(self.parent), "cwd": str(self.root),
                 "permission_mode": "auto", "effort": {"level": "medium"}, "hook_event_name": "PreToolUse",
                 "tool_name": "SubagentHandback", "tool_input": {"message": report},
                 "tool_use_id": "toolu_%d" % self.calls, "agent_id": child}
        if agent is not None:
            event["agent_type"] = agent
        return event

    def run_hooks(self, event, **env):
        """Every PreToolUse command hooks.json runs for this tool; the merged reply."""
        replies = []
        for command in handback_commands():
            done = subprocess.run(["sh", "-c", command], input=json.dumps(event), capture_output=True, text=True,
                                  timeout=30, cwd=str(self.root),
                                  env={**hermetic_env(self.root), "LEOS_AGENT_LOCAL_PATH": str(self.data),
                                       "CLAUDE_PLUGIN_ROOT": str(ROOT), "LEOS_AGENT_DISPATCH_GUARD": "on", **env})
            self.assertEqual(done.returncode, 0, done.stderr)
            replies.append(json.loads(done.stdout) if done.stdout.strip() else {})
        self.assertEqual(len(replies), 1)
        return replies[0]

    def hand_back(self, report, child="c1", agent="leos-agent:leo-cheap", **env):
        """One SubagentHandback call as the host runs it: (delivered, what the child reads back)."""
        event = self.event(report, child, agent)
        reply = self.run_hooks(event, **env)
        decision = (reply.get("hookSpecificOutput") or {}).get("permissionDecision")
        self.assertIn(decision, (None, "deny"))  # this hook never grants or asks
        self.assertNotIn("updatedInput", reply.get("hookSpecificOutput") or {})
        denied = decision == "deny"
        result = reply["hookSpecificOutput"]["permissionDecisionReason"] if denied else "Report delivered to your caller."
        path = self.child_transcript(child)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"id": "m%d" % self.calls, "model": "claude-haiku-4-5", "content": [
                {"type": "tool_use", "id": event["tool_use_id"], "name": "SubagentHandback", "input": event["tool_input"]}]}}) + "\n")
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": event["tool_use_id"], "content": result, **({"is_error": True} if denied else {})}]}}) + "\n")
        return not denied, result

    def markers(self):
        directory = self.data / handback_contract.MARKER_DIR
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


class RefuseOnce(Host):
    def test_a_report_without_its_lines_is_refused_once_then_let_through(self):
        delivered, told = self.hand_back(BARE)
        self.assertFalse(delivered)
        self.assertIn("Result:", told)
        self.assertIn("Verified:", told)
        self.assertEqual(outcome.parse(told), {"outcome": "unknown", "verified": None})  # an echo satisfies nothing
        self.assertEqual(self.hand_back(BARE), (True, "Report delivered to your caller."))
        self.assertEqual(self.hand_back(BARE), (True, "Report delivered to your caller."))
        self.assertEqual(len(self.markers()), 1)
        self.assertFalse(self.hand_back(BARE, child="c2")[0])  # another child gets its own one refusal

    def test_a_worker_that_adds_its_lines_is_recorded_from_the_delivered_report(self):
        self.assertFalse(self.hand_back(BARE)[0])
        self.assertTrue(self.hand_back(CLOSED)[0])
        stop = {"hook_event_name": "SubagentStop", "session_id": "session-1", "agent_id": "c1",
                "agent_type": "leos-agent:leo-cheap", "permission_mode": "auto", "stop_hook_active": False,
                "transcript_path": str(self.parent), "agent_transcript_path": str(self.child_transcript("c1")),
                "last_assistant_message": "Handed back."}
        self.assertIsNone(observe_agent.observe(stop, "claude"))  # delivered: no second nudge
        row = dispatch_log.read()[-1]
        self.assertEqual((row["outcome"], row["verified"], row["outcome_source"]), ("done", True, "handback"))
        self.assertNotIn(b"I fixed the parser", (self.data / "dispatch.jsonl").read_bytes())

    def test_a_report_with_its_lines_passes_untouched(self):
        for report in (CLOSED, "Result: blocked\nVerified: none", "**Result:** partial\n**Verified:** `make test`"):
            with self.subTest(report=report):
                self.assertTrue(self.hand_back(report)[0])
        self.assertEqual(self.markers(), [])

    def test_a_half_contract_is_still_refused(self):
        self.assertFalse(self.hand_back(BARE + "\nResult: done")[0])
        self.assertFalse(self.hand_back(BARE + "\nVerified: ran it", child="c2")[0])
        self.assertFalse(self.hand_back(BARE + "\nResult: <done, partial, blocked or escalate>\nVerified: <the command>",
                                        child="c3")[0])

    def test_markers_are_private_and_stay_bounded(self):
        self.assertFalse(self.hand_back(BARE, child="c0")[0])
        directory = self.data / handback_contract.MARKER_DIR
        self.assertEqual(oct(directory.stat().st_mode & 0o777), oct(0o700))
        stale = directory / ("0" * 64 + ".json")
        stale.write_text("")
        old = time.time() - 3 * 86400
        os.utime(str(stale), (old, old))
        self.assertFalse(self.hand_back(BARE)[0])
        self.assertNotIn(stale.name, self.markers())
        self.assertEqual(len(self.markers()), 2)
        self.assertTrue(all((directory / name).stat().st_size == 0 for name in self.markers()))


class PassThrough(Host):
    def test_other_agents_and_the_main_thread_are_never_refused(self):
        for agent in ("general-purpose", "Explore", "other-plugin:leo-cheap", "leos-agent:leo-unknown", "leo-mystery", None):
            with self.subTest(agent=agent):
                self.assertTrue(self.hand_back(BARE, child="x%d" % self.calls, agent=agent)[0])
        main_thread = {k: v for k, v in self.event(BARE).items() if k != "agent_id"}
        self.assertEqual(self.run_hooks(main_thread), {})
        self.assertEqual(self.markers(), [])

    def test_the_agent_type_falls_back_to_the_transcript_sidecar(self):
        sidecar = self.child_transcript("s1").with_suffix(".meta.json")
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text(json.dumps({"agentType": "leos-agent:leo-premium", "description": "x"}))
        self.assertFalse(self.hand_back(BARE, child="s1", agent=None)[0])
        sidecar.write_text(json.dumps({"agentType": "general-purpose"}))
        self.assertTrue(self.hand_back(BARE, child="s1", agent=None)[0])

    def test_guard_modes_warn_and_off_disable_it(self):
        for n, mode in enumerate(("off", "warn", "0", "false", "disabled", " Off ")):
            with self.subTest(mode=mode):
                self.assertTrue(self.hand_back(BARE, child="m%d" % n, LEOS_AGENT_DISPATCH_GUARD=mode)[0])
        self.assertEqual(self.markers(), [])
        for n, mode in enumerate(("on", "1", "true", "")):
            with self.subTest(mode=mode):
                self.assertFalse(self.hand_back(BARE, child="n%d" % n, LEOS_AGENT_DISPATCH_GUARD=mode)[0])

    def test_nothing_is_refused_when_the_refusal_cannot_be_remembered(self):
        self.data.parent.mkdir(parents=True, exist_ok=True)
        self.data.write_text("a file where the data directory should be")
        self.assertTrue(self.hand_back(BARE)[0])
        self.assertTrue(self.hand_back(BARE)[0])

    def test_unusable_input_fails_open(self):
        for raw in ("", "not json", "[]", json.dumps({"tool_name": "SubagentHandback", "tool_input": "x"})):
            with self.subTest(raw=raw):
                done = subprocess.run([sys.executable, str(ROOT / "scripts" / "handback_contract.py")], input=raw,
                                      capture_output=True, text=True, timeout=30,
                                      env={**hermetic_env(self.root), "LEOS_AGENT_LOCAL_PATH": str(self.data),
                                           "LEOS_AGENT_HARNESS": "claude"})
                self.assertEqual((done.returncode, done.stdout.strip()), (0, "{}"), done.stderr)

    def test_only_its_own_tool_wakes_it_and_the_guard_never_does(self):
        groups = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        mine = [g for g in groups if any("handback_contract.py" in h["command"] for h in g["hooks"])]
        guard = [g for g in groups if any("dispatch_guard.py" in h["command"] for h in g["hooks"])]
        self.assertEqual(len(mine), 1)
        self.assertEqual({t for t in CLAUDE_TOOLS if claude_matches(mine[0]["matcher"], t)}, {"SubagentHandback"})
        self.assertFalse(any(claude_matches(g["matcher"], "SubagentHandback") for g in guard))
        self.assertTrue(all(len(g["hooks"]) == 1 for g in mine + guard))


if __name__ == "__main__":
    unittest.main()
