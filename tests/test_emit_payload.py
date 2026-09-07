"""Behavioral tests for the session-start payload emitter.

The emitter replaced rendering the payload into each harness's global
instruction file. Two properties carry the whole design:

  * it is DETERMINISTIC -- two runs are byte-identical, so the payload sits in
    a cached prompt prefix instead of forcing a full cache write every session;
  * it FAILS OPEN and silent on stdout -- a hook that printed a traceback would
    inject the traceback into the session as context.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EMITTER = ROOT / "scripts" / "emit_payload.py"

sys.path.insert(0, str(ROOT / "scripts"))
import routing  # noqa: E402


def emit(cwd, env=None, stdin=""):
    """Run the emitter the way a hook does: JSON (or junk) on stdin, read stdout."""
    environ = dict(os.environ)
    environ.pop("LEOS_AGENT_HARNESS", None)
    environ.pop("LEOS_AGENT_PAYLOAD", None)
    environ["LEOS_AGENT_ROOT"] = str(ROOT)
    environ.update(env or {})
    return subprocess.run(
        [sys.executable, str(EMITTER)],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        env=environ,
    )


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


class TestHarnessSelection(unittest.TestCase):
    def test_every_harness_emits_its_own_routing_stanza(self):
        config = routing.load()
        for harness in routing.HARNESSES:
            with self.subTest(harness=harness):
                result = emit(ROOT, {"LEOS_AGENT_HARNESS": harness})
                self.assertEqual(result.returncode, 0)
                self.assertIn(routing.stanza(harness, config), result.stdout)

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
