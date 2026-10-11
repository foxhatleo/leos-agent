"""The Codex PreCompact hook only ever answers with a UI warning, or nothing."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compact_nudge.py"
# Codex's pre-compact.command.output schema, additionalProperties false.
OUTPUT_FIELDS = {"continue", "stopReason", "suppressOutput", "systemMessage"}
EVENT = {"hook_event_name": "PreCompact", "trigger": "auto", "session_id": "s", "cwd": "/", "model": "m",
         "transcript_path": None, "turn_id": "t"}


class CompactNudge(unittest.TestCase):
    def run_hook(self, stdin):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            local = Path(tmp) / "local"
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "LEOS_AGENT_LOCAL_PATH": str(local),
                   "CODEX_HOME": str(Path(tmp) / "codex"), "LEOS_AGENT_HARNESS": "codex",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, env=env, capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(os.listdir(tmp), [], "the hook must write nothing")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_a_top_level_compaction_gets_one_warning_naming_handoff(self):
        for trigger in ("manual", "auto"):
            with self.subTest(trigger=trigger):
                out = self.run_hook(json.dumps(dict(EVENT, trigger=trigger)))
                answer = json.loads(out)
                self.assertLessEqual(set(answer), OUTPUT_FIELDS)
                self.assertEqual(set(answer), {"systemMessage"}, "compaction must never be stopped")
                self.assertIn("$leos-agent:handoff", answer["systemMessage"])
                self.assertNotIn("\n", answer["systemMessage"])

    def test_a_subagent_compaction_is_silent(self):
        self.assertEqual(self.run_hook(json.dumps(dict(EVENT, agent_id="a1", agent_type="leo-standard"))), "")

    def test_unreadable_or_foreign_input_is_silent(self):
        for stdin in ("", "{not json", "[]", json.dumps(dict(EVENT, hook_event_name="SessionStart"))):
            with self.subTest(stdin=stdin):
                self.assertEqual(self.run_hook(stdin), "")


if __name__ == "__main__":
    unittest.main()
