"""The opt-in status line: one short line from the status-line JSON and the dispatch log."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import dispatch_log
import statusline

CATALOG = {"schema": 1, "source": "fixture", "fetched_at": 0, "models": [
    {"id": "anthropic/claude-haiku-4.5", "pricing": {"prompt": "0.000001", "completion": "0.000005"}}]}
SESSION = "session-one"


def dispatch(agent, call, session=SESSION, harness="claude"):
    return {"v": 3, "ts": "2026-10-01T10:00:00Z", "harness": harness, "session": dispatch_log.digest(session),
            "decision": "allow", "reason": "x", "tool": "Agent", "agent": agent, "model": None,
            "tier": dispatch_log._tier_of({"agent": agent}), "escalation_from": None, "call_id": call}


def completion(agent, call, usage=None, session=SESSION, model="claude-haiku-4-5"):
    return {"v": 3, "ts": "2026-10-01T10:00:30Z", "harness": "claude", "session": dispatch_log.digest(session),
            "decision": "executed", "agent": agent, "agent_id": None, "call_id": call, "outcome": "done",
            "verified": True, "usage": usage, "usage_complete": True, "turns": 2, "effective_model": model}


def event(**extra):
    base = {"session_id": SESSION, "model": {"id": "claude-opus-4-8", "display_name": "Opus"}}
    base.update(extra)
    return base


class Render(unittest.TestCase):
    def line(self, ev, rows):
        return statusline.render(ev, rows=rows, catalog=CATALOG)

    def test_model_and_session_cost_come_from_the_event_alone(self):
        self.assertEqual(self.line(event(cost={"total_cost_usd": 0.0123}), []), "Opus | session $0.01")
        self.assertEqual(self.line(event(), []), "Opus")
        self.assertEqual(statusline.render({}, rows=[]), "Claude")

    def test_children_are_counted_by_tier_with_the_reference_cost_of_the_logs_rule(self):
        rows = [dispatch("leos-agent:leo-cheap", "a"), dispatch("leos-agent:leo-cheap", "b"),
                dispatch("leos-agent:leo-premium", "c"),
                completion("leos-agent:leo-cheap", "a", {"input": 1000, "output": 200}),
                completion("leos-agent:leo-cheap", "b", {"input": 1000, "output": 200})]
        line = self.line(event(cost={"total_cost_usd": 1.5}), rows)
        # (1000*1e-6 + 200*5e-6) twice = $0.004: the same figure `dispatch_log report` gives.
        summary = dispatch_log.summarise(rows, CATALOG)
        self.assertAlmostEqual(summary["cost"]["cheap"]["reference_usd"][0], 0.004)
        self.assertEqual(line, "Opus | session $1.50 | children cheap 2 premium 1 ref $0.00")

    def test_a_child_that_cannot_be_priced_is_said_so_not_costed(self):
        rows = [dispatch("leos-agent:leo-cheap", "a"),
                completion("leos-agent:leo-cheap", "a", {"input": 5, "output": 5}, model="private-local-model")]
        self.assertEqual(self.line(event(), rows), "Opus | children cheap 1 (1 unpriced)")

    def test_only_this_claude_session_counts(self):
        rows = [dispatch("leos-agent:leo-cheap", "a", session="other"),
                dispatch("leos-agent:leo-cheap", "b", harness="codex")]
        self.assertEqual(self.line(event(), rows), "Opus")

    def test_the_model_name_is_bounded_and_printable(self):
        name = "Opus\x1b[31m" + "x" * 200
        line = self.line(event(model={"display_name": name}), [])
        self.assertTrue(line.isprintable())
        self.assertLessEqual(len(line), 40)

    def test_a_bad_cost_field_is_left_out(self):
        for bad in ("lots", None, True, float("nan"), {"x": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.line(event(cost={"total_cost_usd": bad}), []), "Opus")


class Main(unittest.TestCase):
    def run_main(self, text):
        out = io.StringIO()
        self.assertEqual(statusline.main(io.StringIO(text), out), 0)
        return out.getvalue()

    def test_garbage_input_still_prints_one_line(self):
        for text in ("", "not json", "[]", "null", '{"model": 7}'):
            with self.subTest(text=text):
                self.assertEqual(self.run_main(text), "Claude\n")

    def test_an_unreadable_log_keeps_the_rest_of_the_line(self):
        with mock.patch.object(dispatch_log, "read", side_effect=OSError("denied")):
            out = self.run_main(json.dumps(event(cost={"total_cost_usd": 2})))
        self.assertEqual(out, "Opus | session $2.00\n")

    def test_a_failure_while_rendering_prints_the_model_alone(self):
        with mock.patch.object(statusline, "_usd", side_effect=RuntimeError("boom")):
            out = self.run_main(json.dumps(event(cost={"total_cost_usd": 2})))
        self.assertEqual(out, "Opus\n")


class Process(unittest.TestCase):
    def test_the_script_reads_the_real_log_without_writing_or_touching_the_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local"
            local.mkdir()
            rows = [dispatch("leos-agent:leo-cheap", "a"), dispatch("leos-agent:leo-standard", "b")]
            (local / dispatch_log.LOG_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows))
            before = sorted(p.name for p in local.iterdir())
            env = {k: v for k, v in os.environ.items() if not k.endswith(("_PROXY", "_proxy"))}
            env.update(HOME=tmp, LEOS_AGENT_LOCAL_PATH=str(local), LEOS_AGENT_PRICE_REFRESH="off",
                       PYTHONDONTWRITEBYTECODE="1")
            done = subprocess.run([sys.executable, str(SCRIPTS / "statusline.py")], input=json.dumps(event()),
                                  env=env, capture_output=True, text=True, timeout=30, cwd=tmp)
            self.assertEqual((done.returncode, done.stderr), (0, ""))
            self.assertEqual(done.stdout, "Opus | children cheap 1 standard 1\n")
            self.assertEqual(sorted(p.name for p in local.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
