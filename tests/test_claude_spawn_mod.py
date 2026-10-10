#!/usr/bin/env python3
"""Precedence between the PreToolUse command guard and the agent.spawn mod.

hooks/claude-spawn.js sets LEOS_AGENT_CLAUDE_SPAWN_MOD to its plugin root inside
the Claude Code process. Claude Code starts this plugin's command hooks with
CLAUDE_PLUGIN_ROOT set to the same root; a Bash-tool process inherits the
marker but not CLAUDE_PLUGIN_ROOT (both observed on Claude Code 2.1.296). The
command hook must stand aside only in the first case, so a dispatch is decided
once and the gates still work when run from inside a session.
tests/js/claude-spawn.test.js drives both halves in the order Claude Code runs them.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SpawnModPrecedence(unittest.TestCase):
    def setUp(self):
        self.guard = load("dispatch_guard_spawn_mod", "dispatch_guard.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / "home"
        self.data = home / ".leos-agent-local"
        self.plugin = Path(self.tmp.name) / "plugin"
        self.plugin.mkdir()
        self.env = {
            "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
            "HERMES_HOME": str(home / ".hermes"), "PI_CODING_AGENT_DIR": str(home / ".pi"),
            "OPENCODE_CONFIG_DIR": str(home / ".opencode"), "OPENCODE_CONFIG": str(home / ".opencode" / "o.json"),
            "XDG_CONFIG_HOME": str(home / ".config"), "LEOS_AGENT_LOCAL_PATH": str(self.data),
            "LEOS_AGENT_PRICE_REFRESH": "off",
        }

    def live(self, **extra):
        """This plugin's command hook in a Claude Code process whose mod is live."""
        return dict({"LEOS_AGENT_HARNESS": "claude", self.guard.SPAWN_MOD_ENV: str(self.plugin),
                     "CLAUDE_PLUGIN_ROOT": str(self.plugin)}, **extra)

    def run_hook(self, event, argv=(), **envvars):
        env = dict(self.env, **envvars)
        out, err = io.StringIO(), io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(event).encode("utf-8")))
        cleared = ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "LEOS_AGENT_DISPATCH_GUARD",
                   self.guard.SPAWN_MOD_ENV, "LEOS_AGENT_HARNESS", "CLAUDE_PLUGIN_ROOT")
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in cleared:
                os.environ.pop(name, None)
            os.environ.update(env)
            with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(sys, "stderr", err):
                code = self.guard.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def rows(self):
        path = self.data / "dispatch.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def agent_call(**args):
        return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "toolu_1",
                "session_id": "s", "tool_input": dict({"subagent_type": "leo-cheap", "prompt": "do it"}, **args)}

    def assert_decided(self, result):
        code, out, _ = result
        self.assertEqual(code, 0)
        self.assertIn("model", json.loads(out)["hookSpecificOutput"]["updatedInput"])

    def test_the_command_hook_decides_when_no_mod_is_live(self):
        self.assert_decided(self.run_hook(self.agent_call(), LEOS_AGENT_HARNESS="claude",
                                          CLAUDE_PLUGIN_ROOT=str(self.plugin)))
        self.assertEqual(len(self.rows()), 1)

    def test_the_command_hook_stands_aside_while_its_mod_is_live(self):
        self.assertEqual(self.run_hook(self.agent_call(), **self.live()), (0, "", ""))
        forced = self.live(CLAUDE_CODE_SUBAGENT_MODEL_FORCE="1", CLAUDE_CODE_SUBAGENT_MODEL="opus")
        self.assertEqual(self.run_hook(dict(self.agent_call(), parent_model="claude-haiku-4-5"), **forced),
                         (0, "", ""))
        self.assertEqual(self.rows(), [])

    def test_another_spelling_of_the_same_root_still_counts(self):
        link = Path(self.tmp.name) / "plugin-link"
        link.symlink_to(self.plugin, target_is_directory=True)
        self.assertEqual(self.run_hook(self.agent_call(), **self.live(CLAUDE_PLUGIN_ROOT=str(link) + "/")),
                         (0, "", ""))

    def test_a_process_the_bash_tool_starts_still_gets_decisions(self):
        # The marker is inherited by everything the session starts; only the
        # plugin's own hook process also has CLAUDE_PLUGIN_ROOT.
        bash_child = {"LEOS_AGENT_HARNESS": "claude", self.guard.SPAWN_MOD_ENV: str(self.plugin)}
        self.assert_decided(self.run_hook(self.agent_call(), **bash_child))
        self.assertEqual(len(self.rows()), 1)

    def test_another_plugin_root_does_not_count(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.assert_decided(self.run_hook(self.agent_call(), **self.live(CLAUDE_PLUGIN_ROOT=str(other))))

    def test_the_spawn_hooks_json_call_still_decides_and_logs_once(self):
        code, out, _ = self.run_hook(self.agent_call(), argv=["--json"], **self.live())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["action"], "correct")
        self.assertEqual([row["call_id"] for row in self.rows()], ["toolu_1"])

    def test_the_marker_never_silences_another_harness(self):
        event = {"tool_name": "spawn_agent", "tool_input": {"task_name": "x", "message": "do it"}, "model": "gpt-6-astra"}
        code, _, err = self.run_hook(event, **self.live(LEOS_AGENT_HARNESS="codex"))
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())


if __name__ == "__main__":
    unittest.main()
