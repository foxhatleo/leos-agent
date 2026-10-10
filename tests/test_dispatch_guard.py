#!/usr/bin/env python3
"""The properties that matter for the dispatch guard.

Three of them, in order of what they protect:

A guard that refuses the wrong call is worse than no guard, so most of this file
is about what it must NOT touch -- ordinary tools, MCP tools, malformed
envelopes, and harnesses that cannot name a model in the first place.

A guard that breaks a session is worse still. Every failure path must allow: bad
stdin, a corrupt routing config, an unwritable log, an exception anywhere.

And the log must never become a liability. It lives in a home directory forever,
so a brief's text must not survive into it -- there is a test that plants a
secret in a prompt and asserts the bytes never appear on disk.
"""
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
# A settings file's env reaches Claude Code's Bash tool too, so a gate run in a
# session can inherit these.
CLAUDE_SUBAGENT_SETTINGS = ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dispatch_event(agent="general-purpose", prompt="do a thing", model=None, tool="Agent", **extra):
    args = {"subagent_type": agent, "prompt": prompt}
    if model:
        args["model"] = model
    args.update(extra)
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": args}


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.guard = load("dispatch_guard_test", "dispatch_guard.py")
        self.log = load("dispatch_log_test", "dispatch_log.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()

    def env(self, **extra):
        """The sandboxed data root, without the invoking shell's Claude
        subagent-model settings: the guard honours them."""
        base = {k: v for k, v in os.environ.items() if k not in CLAUDE_SUBAGENT_SETTINGS}
        base.update({"LEOS_AGENT_LOCAL_PATH": str(self.data)}, **extra)
        return mock.patch.dict(os.environ, base, clear=True)

    def write_routing(self, payload):
        (self.data / "routing.json").write_text(json.dumps(payload), encoding="utf-8")

    def run_cli(self, event, **envvars):
        """main() against a throwaway data root. Returns (code, stdout, stderr)."""
        raw = event if isinstance(event, (bytes, str)) else json.dumps(event)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        out, err = io.StringIO(), io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(raw))
        with self.env(**envvars), mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = self.guard.main([])
        return code, out.getvalue(), err.getvalue()

    def with_parent(self, event, model="claude-opus-5-5"):
        """The event as Claude sends it once the parent turn is on disk: the
        guard reads the parent model from the transcript the event names."""
        transcript = Path(self.tmp.name) / "parent.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"model": model}}) + "\n", encoding="utf-8")
        return dict(event, transcript_path=str(transcript))

    def log_lines(self):
        path = self.data / "dispatch.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestShape(GuardCase):
    """What the guard must never touch. Detection is by argument shape, so the
    burden is on proving ordinary tools do not look like a dispatch."""

    def test_ordinary_tools_are_not_dispatches(self):
        for tool, args in (
            ("Read", {"file_path": "/tmp/x"}),
            ("Bash", {"command": "ls", "description": "list"}),
            ("Grep", {"pattern": "foo", "path": "."}),
            ("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}),
        ):
            with self.subTest(tool=tool):
                event = {"tool_name": tool, "tool_input": args}
                self.assertIsNone(self.guard.normalize(event))

    def test_a_prompt_without_an_agent_is_not_a_dispatch(self):
        """spawn_task takes {prompt, title, tldr} and must survive untouched. A
        rule keyed on 'has a prompt' would refuse it, which is why the
        agent-selection field is mandatory."""
        event = {"tool_name": "spawn_task", "tool_input": {
            "prompt": "Fix the stale badge", "title": "Fix badge", "tldr": "noticed in passing"}}
        self.assertIsNone(self.guard.normalize(event))

    def test_a_garbage_model_does_not_count_as_naming_one(self):
        """The escape hatch is naming a model. A non-string in that field is not
        a choice, and must not be mistaken for one."""
        for junk in ([], {}, 0, None, "   "):
            with self.subTest(junk=repr(junk)):
                event = {"tool_name": None, "tool_input": {"agent": "a", "prompt": "p", "model": junk}}
                dispatch = self.guard.normalize(event)
                self.assertIsNotNone(dispatch)
                self.assertIsNone(dispatch.model)

    def test_mcp_tools_are_never_guarded(self):
        event = dispatch_event(tool="mcp__vendor__spawn_agent")
        self.assertIsNone(self.guard.normalize(event))

    def test_every_key_alias_normalizes_the_same(self):
        for agent_key in self.guard.AGENT_KEYS:
            for prompt_key in self.guard.PROMPT_KEYS:
                with self.subTest(agent=agent_key, prompt=prompt_key):
                    event = {"tool_name": "Agent", "tool_input": {agent_key: "explorer", prompt_key: "go"}}
                    dispatch = self.guard.normalize(event)
                    self.assertIsNotNone(dispatch)
                    self.assertEqual(dispatch.agent, "explorer")

    def test_every_envelope_alias_resolves(self):
        for envelope in self.guard.INPUT_KEYS:
            with self.subTest(envelope=envelope):
                event = {"tool_name": "Agent", envelope: {"agent": "x", "brief": "y"}}
                self.assertIsNotNone(self.guard.normalize(event))

    def test_malformed_envelopes_never_raise(self):
        for event in ({}, None, [], "string", 7,
                      {"tool_name": "Agent"},
                      {"tool_name": "Agent", "tool_input": []},
                      {"tool_name": "Agent", "tool_input": {"subagent_type": 5, "prompt": "x"}},
                      {"tool_name": "Agent", "tool_input": {"subagent_type": "   ", "prompt": "x"}}):
            with self.subTest(event=repr(event)[:40]):
                self.assertIsNone(self.guard.normalize(event))


class TestProtocol(GuardCase):
    def test_codex_block_exits_2_with_the_reason_on_stderr(self):
        # Codex's PreToolUse input always carries the active model.
        event = {"tool_name": "spawn_agent", "tool_input": {"message": "Investigate"}, "model": "gpt-6-astra"}
        code, out, err = self.run_cli(event, LEOS_AGENT_HARNESS="codex")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("BLOCKED", err)

    def test_claude_correction_does_not_grant_permission(self):
        code, out, err = self.run_cli(self.with_parent(dispatch_event()))
        self.assertEqual(code, 0)
        response = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(response["updatedInput"]["model"], "sonnet")
        self.assertNotIn("permissionDecision", response)
        self.assertEqual(err, "")

    def test_a_reviewers_lens_is_priced_against_the_reviewer_not_the_root(self):
        # Claude's PreToolUse from inside a subagent names the root transcript
        # and the caller's agent_id; the reviewer's own transcript holds its model.
        root = Path(self.tmp.name) / "root.jsonl"
        root.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5"}}) + "\n", encoding="utf-8")
        reviewer = Path(self.tmp.name) / "root" / "subagents" / "agent-rev1.jsonl"
        reviewer.parent.mkdir(parents=True)
        reviewer.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5-5"}}) + "\n",
                            encoding="utf-8")

        def lens(model=None):
            event = dispatch_event(agent="leos-agent:leo-lens", prompt="Lens: the auth paths at SHA", model=model)
            return dict(event, transcript_path=str(root), agent_id="rev1", session_id="s")

        self.assertEqual(self.run_cli(lens("haiku")), (0, "", ""))
        for model in ("opus", None):
            with self.subTest(model=model):
                code, out, err = self.run_cli(lens(model))
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(json.loads(out)["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
        rows = self.log_lines()
        self.assertEqual([(r["agent"], r["decision"]) for r in rows],
                         [("leos-agent:leo-lens", "allow"), ("leos-agent:leo-lens", "correct"),
                          ("leos-agent:leo-lens", "correct")])

    def test_a_compliant_dispatch_exits_0(self):
        code, _out, err = self.run_cli(dispatch_event(agent="leo-runner"))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_unreadable_stdin_fails_open(self):
        for raw in (b"", b"   ", b"not json at all", b"\xff\xfe\x00garbage", b'"a string"', b"[1,2,3]"):
            with self.subTest(raw=raw[:12]):
                code, _out, _err = self.run_cli(raw)
                self.assertEqual(code, 0)

    def test_off_disables_the_guard_entirely(self):
        code, _out, _err = self.run_cli(dispatch_event(), LEOS_AGENT_DISPATCH_GUARD="off")
        self.assertEqual(code, 0)
        self.assertEqual(self.log_lines(), [])

    def test_warn_records_the_block_without_blocking(self):
        code, _out, err = self.run_cli(self.with_parent(dispatch_event()), LEOS_AGENT_DISPATCH_GUARD="warn")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        rows = self.log_lines()
        self.assertEqual([r["decision"] for r in rows], ["warn"])

    def test_a_log_failure_never_changes_the_decision(self):
        with self.env(), mock.patch.object(self.guard, "_log", side_effect=lambda entry: None):
            result = self.guard.process(self.with_parent(dispatch_event()), "claude")
        self.assertEqual(result["action"], "correct")



class TestTriviality(GuardCase):
    def score(self, prompt):
        return self.guard.triviality(self.guard.normalize(dispatch_event(prompt=prompt)))

    def test_a_one_line_brief_naming_one_file_scores(self):
        self.assertGreaterEqual(self.score("Check scripts/routing.py and report."), 2)

    def test_a_structured_brief_with_many_paths_does_not(self):
        brief = "Goal: audit routing.\n\n" + "\n".join(
            "- inspect scripts/file%d.py for the loader" % n for n in range(12)) + "\n" * 3 + "x" * 1500
        self.assertEqual(self.score(brief), 0)

    def test_triviality_never_changes_the_exit_code(self):
        """A ~15% false-positive rate is fine for a log line and disqualifying
        for a block. This test is what keeps it a log line."""
        code, out, err = self.run_cli(dispatch_event(agent="leo-runner", prompt="run the tests"))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("updatedInput", out)


class TestLog(GuardCase):
    def test_records_round_trip(self):
        with self.env():
            self.log.append({"v": 1, "decision": "allow"})
            self.log.append({"v": 1, "decision": "block"})
            self.assertEqual([r["decision"] for r in self.log.read()], ["allow", "block"])

    def test_prompt_text_never_reaches_disk(self):
        secret = "CLIENT_SECRET_MARKER_do_not_log"
        code, _out, _err = self.run_cli(dispatch_event(prompt="Investigate %s in prod" % secret))
        self.assertEqual(code, 0)
        raw = (self.data / "dispatch.jsonl").read_bytes()
        self.assertNotIn(secret.encode(), raw)
        self.assertIn(b'"prompt"', raw)  # the hash is there; the text is not

    def test_the_working_directory_is_hashed_not_stored(self):
        self.run_cli(dispatch_event(), )
        cwd = "/Users/someone/clients/acme-secret-project"
        self.run_cli(dict(dispatch_event(), cwd=cwd))
        raw = (self.data / "dispatch.jsonl").read_bytes()
        self.assertNotIn(b"acme-secret-project", raw)

    def test_the_log_is_not_world_readable(self):
        with self.env():
            self.log.append({"v": 1, "decision": "allow"})
        mode = stat.S_IMODE(os.stat(self.data / "dispatch.jsonl").st_mode)
        self.assertEqual(mode & 0o077, 0, "log is readable beyond its owner (%o)" % mode)

    def test_rotation_bounds_the_file(self):
        with self.env(), mock.patch.object(self.log, "MAX_BYTES", 400):
            for n in range(40):
                self.log.append({"v": 1, "decision": "allow", "n": n, "pad": "x" * 40})
            self.assertTrue((self.data / "dispatch.jsonl.1").is_file())
            self.assertLess((self.data / "dispatch.jsonl").stat().st_size, 4000)
            self.assertTrue(self.log.read())

    def test_a_truncated_final_line_does_not_break_reading(self):
        with self.env():
            self.log.append({"v": 1, "decision": "allow"})
            with open(self.data / "dispatch.jsonl", "a") as fh:
                fh.write('{"v": 1, "decision": "blo')  # a crash mid-write
            rows = self.log.read()
        self.assertEqual([r["decision"] for r in rows], ["allow"])

    def test_concurrent_appends_do_not_interleave(self):
        def writer(n):
            with self.env():
                self.log.append({"v": 1, "decision": "allow", "n": n, "pad": "y" * 200})

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with self.env():
            rows = self.log.read()
        self.assertEqual(len(rows), 12)
        self.assertEqual(sorted(r["n"] for r in rows), list(range(12)))


class TestEscalationFields(GuardCase):
    def rows(self):
        return [json.loads(line) for line in (self.data / "dispatch.jsonl").read_text().splitlines()]

    def test_a_marked_brief_records_the_tier_and_nothing_else(self):
        secret = "ESCALATION_SECRET_do_not_log"
        self.run_cli(dispatch_event(agent="leo-standard", prompt="Escalation from cheap: %s failed\nFix it" % secret))
        row = self.rows()[-1]
        self.assertEqual(row["escalation_from"], "cheap")
        self.assertEqual(row["tier"], "standard")
        self.assertNotIn(secret.encode(), (self.data / "dispatch.jsonl").read_bytes())

    def test_an_unmarked_brief_is_null_and_an_opaque_one_is_unobservable(self):
        self.run_cli(dispatch_event(agent="leos-agent:leo-cheap", prompt="Read scripts/a.py and report"))
        self.assertIsNone(self.rows()[-1]["escalation_from"])
        self.assertEqual(self.rows()[-1]["tier"], "cheap")
        self.run_cli({"hook_event_name": "PreToolUse", "tool_name": "spawn_agent",
                      "tool_input": {"task_name": "x", "message": "Escalation from cheap: encrypted anyway", "model": "gpt-5.6-luna"}},
                     LEOS_AGENT_HARNESS="codex")
        row = self.rows()[-1]
        self.assertEqual(row["escalation_from"], "unobservable")
        self.assertIsNone(row["tier"])
        self.assertEqual(row["v"], 3)


class TestReport(GuardCase):
    def test_lifecycle_records_do_not_inflate_attempts_or_invent_inheritance(self):
        summary = self.log.summarise([
            {"decision": "allow", "agent": "general"},
            {"decision": "executed", "agent": "general", "effective_model": "haiku"},
        ])
        self.assertEqual(summary["dispatch_attempts"], 1)
        self.assertEqual(summary["confirmed_executions"], 1)
        self.assertNotIn("inherited", summary["tiers"])
        self.assertIn("general @ haiku", summary["agents"])

    def test_a_reconciled_child_is_counted_once_in_its_final_state(self):
        """SessionEnd appends an `executed` row for a child whose model arrived
        late. Its earlier `completed` row is superseded, not a second agent,
        or every late child inflates the unattributed count by one."""
        summary = self.log.summarise([
            {"decision": "completed", "session": "s", "agent_id": "a", "agent": "leo-cheap"},
            {"decision": "executed", "session": "s", "agent_id": "a", "agent": "leo-cheap", "effective_model": "haiku"},
            {"decision": "completed", "session": "s", "agent_id": "b", "agent": "leo-cheap"},
        ])
        self.assertEqual(summary["records"], 3)
        self.assertEqual(summary["superseded"], 1)
        self.assertEqual(summary["decisions"], {"completed": 1, "executed": 1})
        self.assertEqual(summary["agents"], {"leo-cheap @ haiku": 1, "leo-cheap @ unknown": 1})
        self.assertIn("counted once", self.log.render(summary))

    def test_an_unreadable_log_is_an_error_to_callers_and_an_exit_only_in_the_cli(self):
        """read() serves hooks and scanners, which must file the failure and carry
        on; only the CLI should exit. A SystemExit from read() skipped a Codex
        lifecycle hook's mandatory JSON reply and killed the diagnosis bundle."""
        import contextlib
        import io
        import os
        with self.env():
            os.makedirs(self.log.path())
            with self.assertRaises(OSError):
                self.log.read()
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.log.main(["report"])

    def test_rows_without_a_session_or_id_are_never_collapsed(self):
        """Dedupe needs both keys; a bare pair of rows is two observations."""
        summary = self.log.summarise([
            {"decision": "completed", "agent": "x"},
            {"decision": "executed", "agent": "x", "effective_model": "haiku"},
        ])
        self.assertEqual(summary["superseded"], 0)
        self.assertEqual(summary["decisions"], {"completed": 1, "executed": 1})

    def test_a_matching_brief_does_not_prove_execution_or_savings(self):
        """The whole point of hashing the prompt: a block nobody acted on is not
        a saving, and only the hash can tell the two apart."""
        with self.env():
            self.log.append({"v": 1, "decision": "block", "prompt": "abc123", "agent": "general-purpose"})
            self.log.append({"v": 1, "decision": "allow", "prompt": "abc123", "agent": "leo-runner"})
            self.log.append({"v": 1, "decision": "block", "prompt": "def456", "agent": "Explore"})
            summary = self.log.summarise(self.log.read())
        self.assertEqual(summary["blocked"], 2)
        self.assertIsNone(summary["converted"])
        self.assertEqual(summary["confirmed_executions"], 0)

    def test_a_fan_out_is_not_reported_as_lone_small_spawns(self):
        """Parallel small dispatches are the shape the policy wants. Only a
        solitary small spawn is a finding, and the burst key is what separates
        them at read time."""
        with self.env():
            for n in range(4):
                self.log.append({"v": 1, "decision": "allow", "burst": "s:100", "trivial": 3, "prompt": str(n)})
            self.log.append({"v": 1, "decision": "allow", "burst": "s:900", "trivial": 3, "prompt": "lonely"})
            summary = self.log.summarise(self.log.read())
        self.assertEqual(summary["trivial_lone_spawns"], 1)

    def test_a_block_and_its_retry_are_not_mistaken_for_a_fan_out(self):
        """They share a two-second bucket by construction. Counting the block
        toward burst size would let every blocked retry pose as a fan-out and
        silently suppress the finding it should have produced."""
        with self.env():
            self.log.append({"v": 1, "decision": "block", "burst": "s:1", "trivial": 3, "prompt": "p"})
            self.log.append({"v": 1, "decision": "allow", "burst": "s:1", "trivial": 3, "prompt": "p"})
            summary = self.log.summarise(self.log.read())
        self.assertEqual(summary["trivial_lone_spawns"], 1)

    def test_guard_errors_are_distinct_from_allows(self):
        with self.env():
            self.log.append({"v": 1, "decision": "error", "reason": "boom"})
            self.log.append({"v": 1, "decision": "allow"})
            summary = self.log.summarise(self.log.read())
        self.assertEqual(summary["errors"], 1)
        self.assertIn("guard error", self.log.render(summary))


class TestOutcomeReport(GuardCase):
    def d(self, ts, agent, call=None, session="s", harness="claude", **extra):
        row = {"v": 3, "ts": ts, "harness": harness, "session": session, "decision": "allow", "agent": agent,
               "tier": self.log._tier_of({"agent": agent}), "call_id": call, "escalation_from": None}
        row.update(extra)
        return row

    def c(self, ts, agent=None, call=None, session="s", harness="claude", outcome="done", verified=True, **extra):
        row = {"v": 3, "ts": ts, "harness": harness, "session": session, "decision": "completed", "agent": agent,
               "call_id": call, "outcome": outcome, "verified": verified, "outcome_source": "message"}
        row.update(extra)
        return row

    def test_call_id_then_nearest_preceding_then_unmatched(self):
        summary = self.log.summarise([
            self.d("2026-09-21T10:00:00Z", "leo-cheap", call="c1"),
            self.c("2026-09-21T10:01:00Z", "leo-cheap", call="c1", outcome="done"),
            self.d("2026-09-21T10:02:00Z", "leo-standard"),
            self.c("2026-09-21T10:03:00Z", "leo-standard", outcome="escalate", verified=False),
            self.c("2026-09-21T10:04:00Z", None, session="other", outcome="partial", verified=None),
        ])
        self.assertEqual(summary["joins"], {"call_id": 1, "nearest": 1, "unmatched": 1})
        self.assertEqual(summary["outcomes"], {"cheap": {"done": 1}, "standard": {"escalate": 1}, "unrouted": {"partial": 1}})
        self.assertEqual(summary["verified"]["cheap"], {"stated": 1})
        self.assertEqual(summary["verified"]["standard"], {"none": 1})
        self.assertEqual(summary["verified"]["unrouted"], {"unstated": 1})
        text = self.log.render(summary)
        self.assertIn("outcomes", text)
        self.assertIn("cheap 1/1 stated evidence", text)

    def test_ambiguity_is_its_own_bucket_never_a_tier(self):
        summary = self.log.summarise([
            self.d("2026-09-21T10:00:00Z", "leo-cheap"),
            self.d("2026-09-21T10:00:00Z", "leo-cheap"),
            self.c("2026-09-21T10:05:00Z", "leo-cheap", outcome="done"),
        ])
        self.assertEqual(summary["joins"], {"ambiguous": 1})
        self.assertEqual(summary["outcomes"], {"ambiguous": {"done": 1}})

    def test_escalation_chains_and_unobservable_markers(self):
        summary = self.log.summarise([
            self.d("2026-09-21T10:00:00Z", "leo-cheap"),
            self.d("2026-09-21T10:05:00Z", "leo-standard", escalation_from="cheap"),
            self.d("2026-09-21T10:06:00Z", "leo-premium", escalation_from="standard"),
            self.d("2026-09-21T10:07:00Z", "spawn_agent", harness="codex", tier=None, escalation_from="unobservable"),
        ])
        self.assertEqual(summary["escalations"], {"cheap->standard": 1, "standard->premium": 1})
        self.assertEqual(summary["escalation_unobservable"], 1)
        text = self.log.render(summary)
        self.assertIn("cheap -> standard 1", text)
        self.assertIn("unobservable escalation marker", text)

    def test_hermes_children_join_and_silent_harnesses_are_named(self):
        # Hermes's subagent_stop carries the child's session id and no tool
        # call id, so the row joins by the nearest preceding untiered dispatch.
        summary = self.log.summarise([
            self.d("2026-09-21T10:00:00Z", "delegate_task", call="h1", harness="hermes", tier=None),
            self.c("2026-09-21T10:01:01Z", "delegate_task", harness="hermes", outcome="done", reason="hermes-subagent-stop",
                   agent_id="child-session"),
            self.d("2026-09-21T10:02:00Z", "subagent", harness="pi", tier=None),
            self.d("2026-09-21T10:03:00Z", "leo-cheap", harness="cursor"),
            self.c("2026-09-21T10:04:00Z", "leo-cheap", harness="cursor", outcome="unknown", verified=None, outcome_source="status-only", status="completed"),
        ])
        self.assertEqual(summary["outcomes"]["unrouted"], {"done": 1})
        self.assertEqual(summary["silent_harnesses"], ["pi"])
        text = self.log.render(summary)
        self.assertIn("pi: no completion signal observed", text)
        self.assertIn("cursor status-only 1 (no outcome signal available)", text)

    def test_usage_is_summed_per_tier_with_row_counts(self):
        summary = self.log.summarise([
            self.d("2026-09-21T10:00:00Z", "leo-cheap", call="a"),
            self.c("2026-09-21T10:01:00Z", "leo-cheap", call="a", usage={"input": 10, "output": 2}),
            self.d("2026-09-21T10:02:00Z", "leo-cheap", call="b"),
            self.c("2026-09-21T10:03:00Z", "leo-cheap", call="b", usage={"input": 5, "output": 1, "cache_read": 7}),
        ])
        self.assertEqual(summary["usage"], {"cheap": {"input": 15, "output": 3, "cache_read": 7, "rows": 2}})
        self.assertIn("child usage cheap in 15 out 3 cache-read 7 (2 rows)", self.log.render(summary))

    def test_a_pre_instrumentation_log_renders_as_before(self):
        rows = [
            {"decision": "allow", "agent": "leo-cheap", "session": "s"},
            {"decision": "completed", "session": "s", "agent_id": "a", "agent": "leo-cheap"},
            {"decision": "executed", "session": "s", "agent_id": "a", "agent": "leo-cheap", "effective_model": "haiku"},
        ]
        summary = self.log.summarise(rows)
        self.assertEqual((summary["outcomes"], summary["verified"], summary["usage"], summary["escalations"]), ({}, {}, {}, {}))
        self.assertEqual(summary["superseded"], 1)
        self.assertEqual(summary["confirmed_executions"], 1)
        self.assertEqual(summary["pre_instrumentation"], 3)
        text = self.log.render(summary)
        self.assertNotIn("outcomes", text)
        self.assertNotIn("predate", text)


class TestHarnessParity(unittest.TestCase):
    """One policy, five adapters. Each translates an event shape and nothing
    more, so each must reach the same verdict on the same dispatch."""

    def setUp(self):
        self.guard = load("dispatch_guard_parity", "dispatch_guard.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / "routing.json").write_text(json.dumps({"hermes": {"runner": "small"}}), encoding="utf-8")

    def test_hermes_adapter_blocks_exactly_what_the_cli_blocks(self):
        spec = importlib.util.spec_from_file_location("leos_agent_pkg", ROOT / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        cases = [{"tasks": [{"goal": "Investigate failure"}]}, {"action": "list"},
                 {"action": "steer", "subagent_id": "a", "message": "Focus"}]
        with mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.data)}):
            for args in cases:
                self.assertIsNone(module._on_pre_tool_call(tool_name="delegate_task", args=args))

    def test_hermes_adapter_tolerates_an_unexpected_signature(self):
        spec = importlib.util.spec_from_file_location("leos_agent_pkg2", ROOT / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Hermes' real signature is the one thing no version of this repo has
        # verified live, so extra keyword arguments must be absorbed, not raise.
        self.assertIsNone(module._on_pre_tool_call(tool_name="Read", args={"x": 1}, call_id="c", extra=True))

    def test_hermes_registration_matches_public_signature(self):
        spec = importlib.util.spec_from_file_location("leos_agent_registration", ROOT / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        registered = {}
        class Context:
            def register_skill(self, name, path, description=""):
                self.assert_path = Path(path)
                if not self.assert_path.is_file():
                    raise ValueError("skill must be a file")
                registered[name] = path
            def register_hook(self, name, callback):
                registered[name] = callback
            def register_system_prompt_section(self, name, content, position, max_chars):
                text = content({"session_id": "fixture"})
                if not text or len(text) > max_chars:
                    raise ValueError("section must fit")
                registered[name] = text
            def register_command(self, name, callback, description=""):
                registered[name] = callback
        with mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.data)}):
            module.register(Context())
        self.assertIn("leo-review-usage", registered)
        self.assertIn("Cost-aware delegation", registered["leos-agent"])


if __name__ == "__main__":
    unittest.main()
