#!/usr/bin/env python3
"""The Claude dispatch guard as Claude Code drives it.

Claude decides whether to run the hook from hooks.json's matcher, then hands it
the PreToolUse event: the tool name, its input, and the parent transcript. These
tests reproduce that path, so a matcher that wakes the guard for task-list
tools, or a hook that reads the transcript before knowing it has a dispatch,
fails here rather than in a session.
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import routing_engine  # noqa: E402
import session_models  # noqa: E402
from test_dispatch_guard import GuardCase, dispatch_event  # noqa: E402

# Tool names the installed Claude Code offers, including the ones whose names
# contain "Task" or "Agent" without dispatching anything.
CLAUDE_TOOLS = (
    "Agent", "Task", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop", "TaskOutput",
    "TodoWrite", "SendMessage", "ListAgents", "SubagentHandback", "Workflow", "Bash", "Read", "Skill",
    "ToolSearch", "EnterWorktree", "mcp__claude-code-remote__create_session",
    "mcp__ccd_session__spawn_task", "mcp__vendor__delegate_task", "mcp__vendor__dispatch_agent",
)
_EXACT = re.compile(r"[A-Za-z0-9_\- ,|]*")


def claude_matches(matcher, tool):
    """Claude's documented matcher rule for tool events: "*" or "" matches all;
    letters, digits, _, -, spaces, commas and | make an exact-name list; any
    other character makes an unanchored JavaScript regular expression."""
    if matcher in ("", "*"):
        return True
    if _EXACT.fullmatch(matcher):
        return tool in {part.strip() for part in re.split(r"[|,]", matcher)}
    return re.search(matcher, tool) is not None


class ClaudeCase(GuardCase):
    def env(self, **extra):
        """A sandboxed home, no harness config overrides, and none of the
        invoking shell's subagent-model settings."""
        home = Path(self.tmp.name) / "home"
        base = {k: v for k, v in os.environ.items()
                if k not in ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
                             "LEOS_AGENT_DISPATCH_GUARD", "LEOS_AGENT_HARNESS", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                             "HERMES_HOME", "PI_CODING_AGENT_DIR", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG",
                             "XDG_CONFIG_HOME")}
        base.update({"HOME": str(home), "LEOS_AGENT_LOCAL_PATH": str(self.data), "LEOS_AGENT_PRICE_REFRESH": "off"})
        base.update(extra)
        return mock.patch.dict(os.environ, base, clear=True)


class TestMatcher(unittest.TestCase):
    def matchers(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        return [entry["matcher"] for entry in hooks
                if any("dispatch_guard.py" in hook.get("command", "") for hook in entry.get("hooks", []))]

    def test_the_guard_wakes_only_for_the_dispatch_tool(self):
        matched = {tool for tool in CLAUDE_TOOLS for matcher in self.matchers() if claude_matches(matcher, tool)}
        self.assertEqual(matched, {"Agent", "Task"})

    def test_every_tool_the_engine_routes_reaches_the_guard(self):
        for tool in routing_engine.CAPABILITIES["claude"]["tools"]:
            with self.subTest(tool=tool):
                self.assertTrue(any(claude_matches(m, tool) for m in self.matchers()))


class TestToolCheckOrder(ClaudeCase):
    def test_a_non_dispatch_tool_never_reads_the_transcript(self):
        for tool, args in (("TaskCreate", {"subject": "x", "description": "y"}),
                           ("mcp__vendor__spawn_agent", {"subagent_type": "a", "prompt": "b"}),
                           ("SubagentHandback", {"message": "done", "agent": "x"})):
            with self.subTest(tool=tool), self.env(), \
                    mock.patch.object(session_models, "transcript_model", side_effect=AssertionError("read")):
                result = self.guard.process(self.with_parent({"tool_name": tool, "tool_input": args}), "claude")
                self.assertEqual(result["reason"], "not-a-dispatch")
        self.assertEqual(self.log_lines(), [])


class TestGuardMode(ClaudeCase):
    def test_common_falsy_spellings_turn_the_guard_off(self):
        for value in ("off", "0", "false", "False", "no", "disabled", " OFF "):
            with self.subTest(value=value):
                code, out, err = self.run_cli(self.with_parent(dispatch_event()), LEOS_AGENT_DISPATCH_GUARD=value)
                self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.log_lines(), [])

    def test_truthy_spellings_keep_it_on_without_a_diagnostic(self):
        for value in ("on", "1", "true", "yes", "enabled", ""):
            with self.subTest(value=value):
                _code, out, _err = self.run_cli(self.with_parent(dispatch_event()), LEOS_AGENT_DISPATCH_GUARD=value)
                self.assertIn("updatedInput", out)
        self.assertFalse([row for row in self.log_lines() if "diagnostic" in row])

    def test_an_unknown_value_keeps_the_guard_on_and_says_so(self):
        _code, out, _err = self.run_cli(self.with_parent(dispatch_event()), LEOS_AGENT_DISPATCH_GUARD="strict")
        self.assertIn("updatedInput", out)
        self.assertEqual(self.log_lines()[-1]["diagnostic"], "unrecognized-guard-mode")


class TestForks(ClaudeCase):
    def test_a_fork_is_left_alone_and_logged_on_the_parent(self):
        code, out, err = self.run_cli(self.with_parent(dispatch_event(agent="fork"), "claude-opus-5-5"))
        self.assertEqual((code, out, err), (0, "", ""))
        row = self.log_lines()[-1]
        self.assertEqual((row["decision"], row["reason"], row["effective_model"]),
                         ("allow", "fork-inherits-parent", "claude-opus-5-5"))


class TestForcedModel(ClaudeCase):
    def forced(self, event, **env):
        with self.env(CLAUDE_CODE_SUBAGENT_MODEL_FORCE="1", **env):
            return self.guard.process(event, "claude")

    def test_a_forced_setting_never_logs_a_non_dispatch(self):
        for tool, args in (("TaskCreate", {"subject": "x", "description": "y"}),
                           ("mcp__vendor__spawn", {"subagent_type": "a", "prompt": "b"}),
                           ("Agent", "not an object")):
            with self.subTest(tool=tool):
                self.assertEqual(self.forced(self.with_parent({"tool_name": tool, "tool_input": args}))["reason"],
                                 "not-a-dispatch")
        self.assertEqual(self.log_lines(), [])

    def test_forced_models_are_checked_against_the_parent(self):
        event = self.with_parent(dispatch_event(), "claude-sonnet-5-5")
        self.assertEqual(self.forced(event, CLAUDE_CODE_SUBAGENT_MODEL="haiku")["reason"], "forced-model-setting")
        over = self.forced(event, CLAUDE_CODE_SUBAGENT_MODEL="opus")
        self.assertEqual((over["action"], over["reason"]), ("block", "forced-model-over-ceiling"))

    def test_a_fork_stays_on_the_parent_whatever_is_forced(self):
        result = self.forced(self.with_parent(dispatch_event(agent="fork"), "claude-sonnet-5-5"),
                             CLAUDE_CODE_SUBAGENT_MODEL="opus")
        self.assertEqual((result["action"], result["reason"]), ("allow", "fork-inherits-parent"))


class TestErrorRows(ClaudeCase):
    def test_an_exception_is_logged_as_an_enum_and_a_type_name(self):
        secret = "/Users/someone/clients/acme-secret/routing.json"
        with mock.patch.object(routing_engine, "route", side_effect=OSError(secret)):
            code, out, _err = self.run_cli(self.with_parent(dispatch_event()))
        self.assertEqual((code, out), (0, ""))
        row = self.log_lines()[-1]
        self.assertEqual((row["decision"], row["reason"], row["error_type"]), ("error", "guard-error", "OSError"))
        self.assertNotIn(b"acme-secret", (self.data / "dispatch.jsonl").read_bytes())

    def test_unreadable_input_is_its_own_enum(self):
        code, _out, _err = self.run_cli(b'{"tool_name": "Agent", "tool_input": ')
        self.assertEqual(code, 0)
        row = self.log_lines()[-1]
        self.assertEqual((row["decision"], row["reason"], row["error_type"]),
                         ("error", "invalid-hook-input", "JSONDecodeError"))


if __name__ == "__main__":
    unittest.main()
