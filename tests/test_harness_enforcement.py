"""Dispatch enforcement as each host actually delivers it.

Each case reproduces the host's own behaviour from its source or docs: the tool
name Codex puts in hook stdin, how Hermes reads delegate_task's action and what
it does with a raising callback, what Cursor needs back from a permission hook,
and what a malformed routing.json used to switch off everywhere.
"""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pricing  # noqa: E402
import routing  # noqa: E402
import routing_engine as engine  # noqa: E402

CONFIG_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "PI_CODING_AGENT_DIR",
              "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "XDG_CONFIG_HOME")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Sandboxed(unittest.TestCase):
    """HOME, every harness config override, and the data root in a temp dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.data = self.home / "local"
        self.data.mkdir()
        env = {"HOME": str(self.home), "LEOS_AGENT_LOCAL_PATH": str(self.data),
               "LEOS_AGENT_PRICE_REFRESH": "off", "LEOS_AGENT_DISPATCH_GUARD": "on"}
        env.update({name: str(self.home / name.lower()) for name in CONFIG_ENV})
        self.env = env
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The guard honours Claude's subagent-model settings, which a gate run
        # inside a session can inherit.
        for name in ("LEOS_AGENT_HARNESS", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
            os.environ.pop(name, None)
        self.guard = load("dispatch_guard_enforcement", "scripts/dispatch_guard.py")
        self.catalog = json.loads(pricing.BUNDLED.read_text())

    def rows(self):
        path = self.data / "dispatch.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    def write_routing(self, text):
        (self.data / "routing.json").write_text(text, encoding="utf-8")

    def run_guard(self, event, harness):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(event).encode()))
        with mock.patch.dict(os.environ, {"LEOS_AGENT_HARNESS": harness}), mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = self.guard.main([])
        return code, out.getvalue(), err.getvalue()


def codex_matches(pattern, name):
    """Codex's HookMatcher: '*'/empty match all, [A-Za-z0-9_|] is an exact
    alternation, anything else is an unanchored regex search."""
    if pattern in ("", "*"):
        return True
    if re.fullmatch(r"[A-Za-z0-9_|]+", pattern):
        return name in pattern.split("|")
    return re.search(pattern, name) is not None


class CodexHookNames(Sandboxed):
    # Codex flattens a namespaced tool for hooks as namespace + name, and
    # multi-agent v2 registers spawn_agent under the "collaboration" namespace.
    V2 = "collaborationspawn_agent"

    def manifest(self):
        return json.loads((ROOT / "hooks" / "hooks-codex.json").read_text())["hooks"]

    def test_the_manifest_routes_every_spawn_name_and_nothing_else_to_the_guard(self):
        (entry,) = self.manifest()["PreToolUse"]
        for name in ("spawn_agent", "Agent", self.V2):
            with self.subTest(name=name):
                self.assertTrue(codex_matches(entry["matcher"], name))
        for name in ("collaborationsend_message", "collaborationwait_agent", "mcp__vendor__spawn_agent", "spawn_agents"):
            with self.subTest(name=name):
                self.assertFalse(codex_matches(entry["matcher"], name))

    def test_every_name_the_manifest_routes_is_one_the_guard_enforces(self):
        (entry,) = self.manifest()["PreToolUse"]
        for name in ("spawn_agent", self.V2):
            self.assertTrue(codex_matches(entry["matcher"], name))
            with self.subTest(name=name):
                result = engine.route("codex", name, {"task_name": "t", "message": "x"}, "gpt-6-astra",
                                      config={}, catalog=self.catalog)
                self.assertNotEqual(result["reason"], "not-a-dispatch")

    def test_session_start_covers_every_source_codex_sends(self):
        # session-start.command.input.schema.json: startup|resume|clear|compact|fork
        (entry,) = self.manifest()["SessionStart"]
        for source in ("startup", "resume", "clear", "compact", "fork"):
            with self.subTest(source=source):
                self.assertTrue(codex_matches(entry["matcher"], source))

    def test_a_v2_spawn_without_a_model_is_blocked(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": self.V2, "model": "gpt-6-astra",
                 "tool_input": {"task_name": "audit", "message": "encrypted", "fork_turns": "none"}}
        code, out, err = self.run_guard(event, "codex")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("BLOCKED", err)
        self.assertEqual(self.rows()[-1]["decision"], "block")

    def test_a_v2_tier_spawn_reads_agent_type_model_and_effort(self):
        self.write_routing(json.dumps({"codex": {"cheap": {"model": "gpt-6-luna", "effort": "low"}}}))
        args = {"task_name": "scan", "message": "opaque", "fork_turns": "none", "agent_type": "leo-cheap",
                "model": "gpt-6-luna"}
        event = {"hook_event_name": "PreToolUse", "tool_name": self.V2, "model": "gpt-6-astra", "tool_input": args}
        code, _out, err = self.run_guard(event, "codex")
        self.assertEqual(code, 2)
        self.assertIn("reasoning_effort='low'", err)
        code, _out, err = self.run_guard(dict(event, tool_input=dict(args, reasoning_effort="low")), "codex")
        self.assertEqual((code, err), (0, ""))
        row = self.rows()[-1]
        self.assertEqual((row["tool"], row["agent"], row["tier"]), (self.V2, "leo-cheap", "cheap"))
        # The v2 brief is encrypted like v1's: no size, hash or marker is reported.
        self.assertEqual((row["prompt"], row["prompt_bytes"], row["escalation_from"]), (None, 0, "unobservable"))


class HermesAction(Sandboxed):
    """Hermes runs delegate_task as a spawn when (action or "").strip().lower()
    is empty or "spawn"; only list/steer/stop are control actions."""

    def route(self, action):
        args = {"goal": "inspect", "action": action}
        return engine.route("hermes", "delegate_task", args, "claude-haiku-4-5", config={}, catalog=self.catalog,
                            effective_model="claude-opus-5")

    def test_every_spelling_hermes_spawns_is_checked(self):
        for action in (None, "", "Spawn", " SPAWN ", 0):
            with self.subTest(action=action):
                self.assertEqual(self.route(action)["action"], "block")
                event = {"tool_name": "delegate_task", "tool_input": {"goal": "inspect", "action": action}}
                self.assertEqual(self.guard.normalize(event).agent, "delegate_task")

    def test_control_actions_and_values_hermes_never_spawns_are_not_dispatches(self):
        for action in ("list", "steer", " Stop ", "unknown", 5):
            with self.subTest(action=action):
                self.assertEqual(self.route(action)["reason"], "not-a-dispatch")

    def test_the_adapter_blocks_a_spawn_with_a_null_action(self):
        adapter = load("hermes_enforcement_null", "__init__.py")
        adapter._on_request_model(model="claude-haiku-4-5", task_id="t")
        with mock.patch.object(adapter, "_native_delegation_model", return_value="claude-opus-5"):
            result = adapter._on_pre_tool_call(tool_name="delegate_task", args={"goal": "inspect", "action": None},
                                               task_id="t")
        self.assertEqual(result["action"], "block")
        self.assertTrue(result["message"])


class HermesFailsOpen(Sandboxed):
    """Hermes blocks the tool when a pre_tool_call callback raises or outlives
    plugins.hook_callback_timeout (30 s by default). The guard must not."""

    def setUp(self):
        super().setUp()
        self.adapter = load("hermes_enforcement_open", "__init__.py")
        native = mock.patch.object(self.adapter, "_native_delegation_model", return_value=None)
        native.start()
        self.addCleanup(native.stop)

    def call(self):
        return self.adapter._on_pre_tool_call(tool_name="delegate_task", args={"goal": "inspect"}, task_id="t")

    def test_any_bridge_exception_allows_and_says_so(self):
        for failure in (RuntimeError("boom"), TypeError("not serializable"), MemoryError(),
                        subprocess.TimeoutExpired("python3", 10)):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(self.adapter.subprocess, "run", side_effect=failure), \
                    self.assertLogs(self.adapter.logger, "WARNING"):
                self.assertIsNone(self.call())

    def test_unexpected_guard_output_allows(self):
        for stdout in ("[]", '"block"', "{}", '{"action": "block"}', "null"):
            completed = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
            with self.subTest(stdout=stdout), mock.patch.object(self.adapter.subprocess, "run", return_value=completed):
                result = self.call()
                # A block that names no reason still carries a non-empty message.
                self.assertTrue(result is None or (result["action"] == "block" and result["message"]))

    def test_an_unreadable_delegation_config_still_runs_the_guard(self):
        with mock.patch.object(self.adapter, "_native_delegation_model", side_effect=RuntimeError("yaml")), \
                self.assertLogs(self.adapter.logger, "WARNING"):
            self.assertIsNone(self.call())
        self.assertEqual(self.rows()[-1]["reason"], "per-dispatch-routing-unavailable")

    def test_the_bridge_is_bounded_inside_the_hermes_default(self):
        seen = {}

        def run(*args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            raise subprocess.TimeoutExpired("python3", kwargs.get("timeout"))
        with mock.patch.object(self.adapter.subprocess, "run", side_effect=run), \
                self.assertLogs(self.adapter.logger, "WARNING"):
            self.assertIsNone(self.call())
        self.assertLessEqual(seen["timeout"], 10)


class InvalidRoutingConfig(Sandboxed):
    TYPO = json.dumps({"claude": {"standrad": "sonnet"}, "codex": {"cheap": "gpt-6-luna-custom"}})

    def test_a_typo_still_routes_with_defaults_and_names_the_cause(self):
        self.write_routing(self.TYPO)
        event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "parent_model": "opus",
                 "tool_input": {"subagent_type": "general-purpose", "prompt": "Investigate"}}
        code, out, _err = self.run_guard(event, "claude")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
        row = self.rows()[-1]
        self.assertEqual((row["decision"], row["diagnostic"]), ("correct", "routing-config-invalid"))

    def test_sections_that_still_validate_are_kept(self):
        self.write_routing(self.TYPO)
        result = engine.route("codex", "spawn_agent", {"agent_type": "leo-cheap", "message": "x"}, "gpt-6-astra",
                              catalog=self.catalog)
        self.assertEqual((result["action"], result["diagnostic"]), ("block", "routing-config-invalid"))
        self.assertIn("gpt-6-luna-custom", result["retry"])

    def test_unparseable_json_routes_with_defaults(self):
        self.write_routing("{not json")
        result = engine.route("claude", "Agent", {"subagent_type": "leo-cheap", "prompt": "x"}, "opus",
                              catalog=self.catalog)
        self.assertEqual((result["updated_input"]["model"], result["diagnostic"]), ("haiku", "routing-config-invalid"))

    def test_a_valid_config_carries_no_diagnostic(self):
        self.write_routing(json.dumps({"claude": {"cheap": "haiku"}}))
        event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "parent_model": "opus",
                 "tool_input": {"subagent_type": "leo-cheap", "prompt": "Investigate"}}
        self.run_guard(event, "claude")
        self.assertNotIn("diagnostic", self.rows()[-1])

    def test_the_policy_is_still_emitted_and_the_cause_logged(self):
        self.write_routing(self.TYPO)
        env = dict(os.environ, LEOS_AGENT_ROOT=str(ROOT))
        # The broken claude section renders defaults; the valid codex one is kept.
        kept = routing.validate({"codex": json.loads(self.TYPO)["codex"]})
        for harness, config in (("claude", {}), ("codex", kept)):
            with self.subTest(harness=harness):
                result = subprocess.run([sys.executable, str(ROOT / "scripts" / "emit_payload.py")], input="",
                                        capture_output=True, text=True, env=dict(env, LEOS_AGENT_HARNESS=harness))
                self.assertEqual(result.returncode, 0)
                self.assertIn(routing.stanza(harness, config), result.stdout)
        log = (self.data / "emit-payload.log").read_text(encoding="utf-8")
        self.assertIn("routing-config-invalid", log)


class OpenCodeCorrectionRow(Sandboxed):
    PROFILES = {"leo-standard": {"model": "openai/gpt-5.6-terra"}, "leo-parent": {"model": None}}

    def event(self):
        return {"tool_name": "task", "parent_model": "openai/gpt-5.6-luna", "session_id": "s", "call_id": "c",
                "tool_input": {"subagent_type": "general", "prompt": "Investigate", "description": "d"},
                "native_profiles": self.PROFILES}

    def test_the_row_names_the_agent_that_runs(self):
        result = self.guard.process(self.event(), "opencode")
        self.assertEqual(result["action"], "correct")
        row = self.rows()[-1]
        self.assertEqual((row["agent"], row["tier"]), (result["updated_input"]["subagent_type"], "parent"))

    def test_warn_mode_keeps_the_requested_agent(self):
        with mock.patch.dict(os.environ, {"LEOS_AGENT_DISPATCH_GUARD": "warn"}):
            self.guard.process(self.event(), "opencode")
        row = self.rows()[-1]
        self.assertEqual((row["decision"], row["agent"], row["tier"]), ("warn", "general", None))


class CursorPermission(Sandboxed):
    """Cursor blocks a permission hook on invalid JSON or an invalid response,
    even with failClosed off, so subagentStart must always answer."""

    def setUp(self):
        super().setUp()
        self.hook = load("cursor_hook_enforcement", "scripts/cursor_hook.py")

    def run_hook(self, raw, argv=("subagentStart",)):
        out = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(raw if isinstance(raw, bytes) else raw.encode()))
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out):
            self.assertEqual(self.hook.main(list(argv)), 0)
        return out.getvalue()

    def start(self, child, parent="claude-haiku-4-5"):
        return json.dumps({"hook_event_name": "subagentStart", "subagent_type": "generalPurpose", "task": "Inspect",
                           "subagent_model": child, "model_id": parent, "conversation_id": "c", "tool_call_id": "t"})

    def test_an_allowed_start_answers_allow_and_nothing_more(self):
        self.assertEqual(json.loads(self.run_hook(self.start("claude-haiku-4-5", "claude-opus-5"))), {"permission": "allow"})

    def test_a_deny_tells_the_model_why(self):
        response = json.loads(self.run_hook(self.start("claude-opus-5")))
        self.assertEqual(response["permission"], "deny")
        self.assertIn("parent", response["agent_message"])
        self.assertEqual(response["user_message"], response["agent_message"])

    def test_failures_still_answer_allow(self):
        for raw in (b"", b"not json", b"[1]", b"\xff\xfe"):
            with self.subTest(raw=raw):
                self.assertEqual(json.loads(self.run_hook(raw)), {"permission": "allow"})
        with mock.patch.object(self.hook.dispatch_guard, "process", side_effect=RuntimeError("boom")):
            self.assertEqual(json.loads(self.run_hook(self.start("claude-opus-5"), argv=())), {"permission": "allow"})

    def test_the_manifest_names_the_permission_event(self):
        hooks = json.loads((ROOT / "hooks" / "hooks-cursor.json").read_text())["hooks"]
        (entry,) = hooks["subagentStart"]
        self.assertIn("subagentStart", entry["command"].split()[2:])


if __name__ == "__main__":
    unittest.main()
