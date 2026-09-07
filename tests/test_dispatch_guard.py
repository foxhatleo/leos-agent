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
        base = {"LEOS_AGENT_LOCAL_PATH": str(self.data)}
        base.update(extra)
        return mock.patch.dict(os.environ, base)

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


class TestDecision(GuardCase):
    def decide(self, event, harness="claude"):
        with self.env():
            dispatch = self.guard.normalize(event)
            return self.guard.decide(dispatch, harness, self.guard.routable(harness))[0]

    def test_generic_agent_without_a_model_is_blocked(self):
        self.assertEqual(self.decide(dispatch_event()), self.guard.BLOCK)

    def test_an_explicit_model_is_always_allowed(self):
        """Naming the model IS the statement that inheriting was intended. The
        guard never judges which model was right."""
        self.assertEqual(self.decide(dispatch_event(model="claude-opus-5")), self.guard.ALLOW)

    def test_leo_tiers_carry_their_own_model(self):
        for agent in ("leo-runner", "leo-executor"):
            with self.subTest(agent=agent):
                self.assertEqual(self.decide(dispatch_event(agent=agent)), self.guard.ALLOW)

    def test_a_namespaced_tier_is_still_a_tier(self):
        """A plugin install namespaces the type: Claude Code dispatches
        `leos-agent:leo-runner`, not `leo-runner`. 10.7.0 tested the bare form
        only and shipped a guard that refused the very path its own refusal
        message recommends -- every tier dispatch blocked on a plugin install."""
        for agent in ("leo-runner", "leo-executor",
                      "leos-agent:leo-runner", "leos-agent:leo-executor",
                      "leos-agent/leo-runner"):
            with self.subTest(agent=agent):
                self.assertEqual(self.decide(dispatch_event(agent=agent)), self.guard.ALLOW)

    def test_a_namespace_alone_does_not_make_a_tier(self):
        """Stripping the namespace must not turn any namespaced agent into a
        tier -- only one whose bare name really is leo-*."""
        for agent in ("leos-agent:general-purpose", "other:Explore", "leonardo", "leos-runner"):
            with self.subTest(agent=agent):
                self.assertEqual(self.decide(dispatch_event(agent=agent)), self.guard.BLOCK)

    def test_a_model_routed_dispatch_is_seen_without_an_agent_key(self):
        """Codex's spawn_agent takes {task_name, message, fork_turns, model,
        reasoning_effort}. task_name is a free-text label, so `model` is the
        whole routing decision and the agent-key rule alone would never see it."""
        base = {"task_name": "rose_shared", "message": "gAAAAABopaque", "fork_turns": "none"}
        event = {"tool_name": "spawn_agent", "tool_input": base}
        self.assertEqual(self.decide(event, "codex"), self.guard.BLOCK)
        with_model = {"tool_name": "spawn_agent", "tool_input": dict(base, model="gpt-5.6-luna")}
        self.assertEqual(self.decide(with_model, "codex"), self.guard.ALLOW)

    def test_an_opaque_brief_is_not_measured(self):
        """Codex encrypts `message`. Its length is a proxy at best and its hash
        changes on every re-send, so neither the size heuristic nor the
        conversion hash may pretend to mean something there."""
        d = self.guard.normalize({"tool_name": "spawn_agent", "tool_input": {
            "task_name": "x", "message": "gAAAAAB" + "z" * 400}})
        self.assertTrue(d.opaque)
        self.assertEqual(self.guard.triviality(d), 0)
        self.assertIsNone(d.prompt_hash)

    def test_an_unknown_tool_still_needs_an_agent_key(self):
        """Naming spawn_agent must not loosen the rule for everything else --
        spawn_task carries a prompt and no agent and stays invisible."""
        self.assertIsNone(self.guard.normalize({"tool_name": "spawn_task", "tool_input": {
            "prompt": "Fix the badge", "title": "t", "tldr": "x"}}))

    def test_the_codex_refusal_does_not_recommend_an_agent_it_cannot_name(self):
        d = self.guard.normalize({"tool_name": "spawn_agent", "tool_input": {
            "task_name": "x", "message": "gAAAA"}})
        message = self.guard.render_block(d, "codex")
        self.assertIn("model", message)
        self.assertNotIn("subagent_type", message)

    def test_a_harness_that_cannot_route_is_never_blocked(self):
        """The payload itself tells such a harness to inherit and say so, so a
        block there would demand something impossible."""
        with self.env():
            self.assertFalse(self.guard.routable("opencode"))
            self.assertTrue(self.guard.routable("codex"))
        self.assertEqual(self.decide(dispatch_event(), "opencode"), self.guard.ALLOW)

    def test_a_configured_harness_becomes_routable(self):
        self.write_routing({"opencode": {"runner": "anthropic/claude-haiku-4-5"}})
        with self.env():
            self.assertTrue(self.guard.routable("opencode"))
        self.assertEqual(self.decide(dispatch_event(), "opencode"), self.guard.BLOCK)

    def test_a_corrupt_routing_config_allows_rather_than_exits(self):
        """routing.py exits the process on a malformed config. A hook that
        inherited that would take the session with it."""
        # opencode, not codex: codex is routable without consulting the config,
        # so it would never exercise the parse at all.
        (self.data / "routing.json").write_text("{not json", encoding="utf-8")
        with self.env():
            self.assertFalse(self.guard.routable("opencode"))
        self.assertEqual(self.decide(dispatch_event(), "opencode"), self.guard.ALLOW)


class TestProtocol(GuardCase):
    def test_a_block_exits_2_with_the_reason_on_stderr(self):
        code, out, err = self.run_cli(dispatch_event())
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("BLOCKED", err)

    def test_the_block_message_names_every_remedy(self):
        _code, _out, err = self.run_cli(dispatch_event())
        for remedy in ("leo-runner", "leo-executor", 'model: "<name>"', "LEOS_AGENT_DISPATCH_GUARD=off"):
            self.assertIn(remedy, err)

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
        code, _out, err = self.run_cli(dispatch_event(), LEOS_AGENT_DISPATCH_GUARD="warn")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        rows = self.log_lines()
        self.assertEqual([r["decision"] for r in rows], ["block"])

    def test_a_log_failure_never_changes_the_decision(self):
        with mock.patch.object(self.log, "append", side_effect=OSError("read-only home")):
            code, _out, err = self.run_cli(dispatch_event())
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED", err)


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
        self.assertIn("systemMessage", out)


class TestLog(GuardCase):
    def test_records_round_trip(self):
        with self.env():
            self.log.append({"v": 1, "decision": "allow"})
            self.log.append({"v": 1, "decision": "block"})
            self.assertEqual([r["decision"] for r in self.log.read()], ["allow", "block"])

    def test_prompt_text_never_reaches_disk(self):
        secret = "CLIENT_SECRET_MARKER_do_not_log"
        code, _out, _err = self.run_cli(dispatch_event(prompt="Investigate %s in prod" % secret))
        self.assertEqual(code, 2)
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


class TestReport(GuardCase):
    def test_a_blocked_brief_that_returns_with_a_tier_counts_as_converted(self):
        """The whole point of hashing the prompt: a block nobody acted on is not
        a saving, and only the hash can tell the two apart."""
        with self.env():
            self.log.append({"v": 1, "decision": "block", "prompt": "abc123", "agent": "general-purpose"})
            self.log.append({"v": 1, "decision": "allow", "prompt": "abc123", "agent": "leo-runner"})
            self.log.append({"v": 1, "decision": "block", "prompt": "def456", "agent": "Explore"})
            summary = self.log.summarise(self.log.read())
        self.assertEqual(summary["blocked"], 2)
        self.assertEqual(summary["converted"], 1)

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

        cases = [
            ({"subagent_type": "general-purpose", "prompt": "go"}, True),
            ({"subagent_type": "leo-runner", "prompt": "go"}, False),
            ({"subagent_type": "general-purpose", "prompt": "go", "model": "small"}, False),
            ({"file_path": "/tmp/x"}, False),
        ]
        with mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.data)}):
            for args, expect_block in cases:
                with self.subTest(args=args):
                    result = module._on_pre_tool_call(tool_name="Agent", args=args)
                    self.assertEqual(result is not None, expect_block)
                    if expect_block:
                        self.assertEqual(result["action"], "block")
                        self.assertIn("BLOCKED", result["message"])

    def test_hermes_adapter_tolerates_an_unexpected_signature(self):
        spec = importlib.util.spec_from_file_location("leos_agent_pkg2", ROOT / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Hermes' real signature is the one thing no version of this repo has
        # verified live, so extra keyword arguments must be absorbed, not raise.
        self.assertIsNone(module._on_pre_tool_call(tool_name="Read", args={"x": 1}, call_id="c", extra=True))

    def test_the_opencode_prefilter_uses_the_same_keys(self):
        """index.js duplicates the key lists because OpenCode has no matcher and
        a JS-side prefilter is what stops a python3 spawn per Read. Duplication
        is acceptable only while it is pinned."""
        js = (ROOT / "index.js").read_text(encoding="utf-8")
        for name, expected in (("AGENT_KEYS", self.guard.AGENT_KEYS), ("PROMPT_KEYS", self.guard.PROMPT_KEYS)):
            with self.subTest(name=name):
                block = js.split("const %s = [" % name, 1)[1].split("]", 1)[0]
                found = tuple(part.strip().strip("'\"") for part in block.split(",") if part.strip())
                self.assertEqual(found, tuple(expected))


if __name__ == "__main__":
    unittest.main()
