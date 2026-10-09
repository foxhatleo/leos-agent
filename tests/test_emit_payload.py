"""Behavioral tests for the session-start payload emitter.

The emitter replaced rendering the payload into each harness's global
instruction file. Two properties carry the whole design:

  * it is DETERMINISTIC -- two runs are byte-identical, so the payload sits in
    a cached prompt prefix instead of forcing a full cache write every session;
  * it FAILS OPEN and silent on stdout -- a hook that printed a traceback would
    inject the traceback into the session as context.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EMITTER = ROOT / "scripts" / "emit_payload.py"

sys.path.insert(0, str(ROOT / "scripts"))
import routing  # noqa: E402

CONFIG_OVERRIDES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "PI_CODING_AGENT_DIR",
                    "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "XDG_CONFIG_HOME")


def emit(cwd, env=None, stdin=""):
    """Run the emitter the way a hook does: JSON (or junk) on stdin, read stdout.

    HOME, the data root, and every harness config override point into a
    throwaway directory unless the test names its own data root.
    """
    with tempfile.TemporaryDirectory() as sandbox:
        environ = dict(os.environ)
        environ.pop("LEOS_AGENT_HARNESS", None)
        environ.pop("LEOS_AGENT_PAYLOAD", None)
        for name in CONFIG_OVERRIDES:
            environ.pop(name, None)
        environ.update({"HOME": sandbox, "LEOS_AGENT_LOCAL_PATH": os.path.join(sandbox, "local"),
                        "LEOS_AGENT_PRICE_REFRESH": "off", "LEOS_AGENT_ROOT": str(ROOT)})
        environ.update(env or {})
        return subprocess.run(
            [sys.executable, str(EMITTER)],
            cwd=str(cwd),
            input=stdin,
            capture_output=True,
            text=True,
            env=environ,
        )


def claude_session_start_rows(messages, new_output):
    """Claude Code's handling of a SessionStart hook's stdout on resume.

    A reduced model of the installed CLI (2.1.296): each SessionStart row
    already in the resumed conversation contributes its content as a key, and a
    new row whose content matches a key is dropped instead of appended.
    """
    seen = {m["content"] for m in messages if m.get("hookEvent") == "SessionStart" and m.get("content")}
    row = {"type": "hook_success", "hookEvent": "SessionStart", "content": new_output}
    return [] if new_output == "" or new_output in seen else [row]


class TestDeterminism(unittest.TestCase):
    def test_two_runs_are_byte_identical(self):
        first = emit(ROOT, {"LEOS_AGENT_HARNESS": "claude"})
        second = emit(ROOT, {"LEOS_AGENT_HARNESS": "claude"})
        self.assertEqual(first.returncode, 0)
        self.assertTrue(first.stdout.strip())
        self.assertEqual(first.stdout, second.stdout)

    def test_the_working_directory_does_not_change_the_output(self):
        # The emitter runs from wherever the harness happens to start it; if cwd
        # leaked into the text, every project would get its own cache entry.
        with tempfile.TemporaryDirectory() as tmp:
            here = emit(ROOT, {"LEOS_AGENT_HARNESS": "claude"})
            there = emit(tmp, {"LEOS_AGENT_HARNESS": "claude"})
        self.assertEqual(here.stdout, there.stdout)

    def test_no_absolute_path_reaches_the_payload(self):
        for harness in routing.HARNESSES:
            with self.subTest(harness=harness):
                out = emit(ROOT, {"LEOS_AGENT_HARNESS": harness}).stdout
                self.assertNotIn("/Users/", out)
                self.assertNotIn("/home/", out)
                self.assertNotIn(str(ROOT), out)


class TestResume(unittest.TestCase):
    """A resume re-runs SessionStart; the policy must not pile up in context."""

    def event(self, **fields):
        base = {"session_id": "s-1", "hook_event_name": "SessionStart", "cwd": str(ROOT)}
        base.update(fields)
        return json.dumps(base)

    def test_a_resume_adds_no_second_copy_of_an_unchanged_policy(self):
        startup = emit(ROOT, {"LEOS_AGENT_HARNESS": "claude"}, stdin=self.event(
            source="startup", model="claude-opus-5-5", transcript_path="/tmp/a.jsonl"))
        # Resume events carry fields a startup lacks; none may reach the output.
        resumed = emit(ROOT, {"LEOS_AGENT_HARNESS": "claude"}, stdin=self.event(
            source="resume", model="claude-sonnet-5-5", transcript_path="/tmp/b.jsonl",
            seconds_since_last_response=5400, context_tokens=182340,
            prompt_cache_likely_expired=True, estimated_cache_write_usd=1.14))
        self.assertEqual(startup.returncode, 0)
        self.assertTrue(startup.stdout.strip())
        conversation = claude_session_start_rows([], startup.stdout)
        self.assertEqual(len(conversation), 1)
        self.assertEqual(claude_session_start_rows(conversation, resumed.stdout), [])

    def test_a_changed_policy_is_still_delivered_on_resume(self):
        # The reason `resume` stays in the matcher: an upgrade between sessions
        # must reach a resumed conversation.
        current = emit(ROOT, {"LEOS_AGENT_HARNESS": "claude"}, stdin=self.event(source="resume"))
        stale = [{"type": "hook_success", "hookEvent": "SessionStart", "content": "an older policy\n"}]
        self.assertEqual(len(claude_session_start_rows(stale, current.stdout)), 1)


class TestBreadcrumbLog(unittest.TestCase):
    def unknown_harness(self, local):
        return emit(ROOT, {"LEOS_AGENT_HARNESS": "nosuchharness", "LEOS_AGENT_LOCAL_PATH": str(local)})

    def test_the_log_rotates_past_one_mebibyte_and_keeps_one_older_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            log = local / "emit-payload.log"
            log.write_bytes(b"old line\n" * (1 + (1 << 20) // 9))
            self.assertEqual(self.unknown_harness(local).stdout, "")
            self.assertTrue((local / "emit-payload.log.1").read_bytes().startswith(b"old line\n"))
            self.assertIn("nosuchharness", log.read_text(encoding="utf-8"))
            self.assertLess(log.stat().st_size, 1024)

            log.write_bytes(b"newer line\n" * (1 + (1 << 20) // 11))
            self.unknown_harness(local)
            self.assertTrue((local / "emit-payload.log.1").read_bytes().startswith(b"newer line\n"))
            self.assertEqual(sorted(p.name for p in local.glob("emit-payload.log.[0-9]*")), ["emit-payload.log.1"])

    def test_a_small_log_is_appended_to_and_a_new_one_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            self.unknown_harness(local)
            self.unknown_harness(local)
            log = local / "emit-payload.log"
            self.assertEqual(log.read_text(encoding="utf-8").count("nosuchharness"), 2)
            self.assertFalse((local / "emit-payload.log.1").exists())
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)


class TestHarnessSelection(unittest.TestCase):
    def test_every_harness_emits_its_own_routing_stanza(self):
        # emit() sandboxes the data root, so the routing config is the default.
        for harness in routing.HARNESSES:
            with self.subTest(harness=harness):
                result = emit(ROOT, {"LEOS_AGENT_HARNESS": harness})
                self.assertEqual(result.returncode, 0)
                self.assertIn(routing.stanza(harness, {}), result.stdout)

    def test_an_unparseable_stdin_still_emits(self):
        # A harness whose event envelope we have never seen must not cost the
        # session its policy.
        result = emit(ROOT, {"LEOS_AGENT_HARNESS": "claude"}, stdin="not json at all")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.strip())

    def test_an_unknown_harness_emits_nothing_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = emit(ROOT, {"LEOS_AGENT_HARNESS": "nosuchharness", "LEOS_AGENT_LOCAL_PATH": tmp})
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("nosuchharness", (Path(tmp) / "emit-payload.log").read_text(encoding="utf-8"))


class TestFailsOpen(unittest.TestCase):
    def test_the_off_switch_emits_nothing(self):
        result = emit(ROOT, {"LEOS_AGENT_HARNESS": "claude", "LEOS_AGENT_PAYLOAD": "off"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_a_missing_payload_exits_clean_and_leaves_a_breadcrumb(self):
        # A bad LEOS_AGENT_ROOT is NOT enough to produce this: plugin_root()
        # rejects an override whose rules/preferences.md is absent and falls
        # back to the emitter's own location, which is the real plugin. To get
        # a rootless emitter you have to run a copy that has no payload beside
        # it -- which is what a half-deleted plugin cache looks like.
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "plugin"
            shutil.copytree(ROOT / "scripts", broken / "scripts")
            local = Path(tmp) / "local"
            environ = dict(os.environ)
            environ.pop("LEOS_AGENT_ROOT", None)
            environ.update({"LEOS_AGENT_HARNESS": "claude", "LEOS_AGENT_LOCAL_PATH": str(local)})
            result = subprocess.run(
                [sys.executable, str(broken / "scripts" / "emit_payload.py")],
                cwd=tmp,
                input="",
                capture_output=True,
                text=True,
                env=environ,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("Traceback", result.stdout)
            self.assertTrue((local / "emit-payload.log").is_file())

    def test_a_frontmatter_only_payload_exits_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "plugin"
            (broken / "rules").mkdir(parents=True)
            (broken / "rules" / "preferences.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
            (broken / "package.json").write_text('{"version": "1.0.0"}\n', encoding="utf-8")
            local = Path(tmp) / "local"
            result = emit(
                ROOT,
                {
                    "LEOS_AGENT_HARNESS": "claude",
                    "LEOS_AGENT_ROOT": str(broken),
                    "LEOS_AGENT_LOCAL_PATH": str(local),
                },
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("Traceback", result.stdout)


if __name__ == "__main__":
    unittest.main()
