"""The paired benchmark's statistics, parsers, budget gate and cleanup, offline.

Every `bench.py run` is a paid model call, so nothing here runs one: the eval
CLI's output is reproduced in the shape Claude Code 2.1.296 writes it (the
`--json` result and a run trace ending in its `result` record).
"""
import itertools
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bench  # noqa: E402

_CONFIG_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "PI_CODING_AGENT_DIR",
               "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "XDG_CONFIG_HOME", "LEOS_AGENT_LOCAL_PATH")
_SANDBOX = None
_ENV = None


def setUpModule():
    global _SANDBOX, _ENV
    _SANDBOX = tempfile.TemporaryDirectory()
    env = {k: v for k, v in os.environ.items() if k not in _CONFIG_ENV}
    env.update(HOME=_SANDBOX.name, LEOS_AGENT_LOCAL_PATH=os.path.join(_SANDBOX.name, "local"))
    _ENV = mock.patch.dict(os.environ, env, clear=True)
    _ENV.start()


def tearDownModule():
    _ENV.stop()
    _SANDBOX.cleanup()


def brute_force_p(deltas):
    """Two-sided signed-rank p by enumerating every sign assignment."""
    nonzero = [d for d in deltas if d != 0]
    if not nonzero:
        return 1.0
    ranks = bench.average_ranks([abs(d) for d in nonzero])
    observed = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    stats = [sum(r for r, s in zip(ranks, signs) if s) for signs in itertools.product((0, 1), repeat=len(ranks))]
    low = sum(1 for s in stats if s <= observed + 1e-9) / len(stats)
    high = sum(1 for s in stats if s >= observed - 1e-9) / len(stats)
    return min(1.0, 2 * min(low, high))


class Statistics(unittest.TestCase):
    def test_exact_p_matches_enumeration_with_ties_and_zeros(self):
        samples = [
            [0.12, -0.03, 0.05, 0.2, 0.07, 0.01],
            [1, 1, 2, -2, 3, 3, 3, -1, 0, 4],
            [-0.5, -0.4, -0.3, 0.1, -0.2, -0.6, -0.7, 0, -0.1],
            [2, -2, 2, -2],
            [5],
        ]
        for deltas in samples:
            with self.subTest(deltas=deltas):
                self.assertAlmostEqual(bench.wilcoxon(deltas)["p"], brute_force_p(deltas), places=12)

    def test_textbook_values(self):
        # Six differences all in one direction: the smallest possible p at n=6.
        result = bench.wilcoxon([1, 2, 3, 4, 5, 6])
        self.assertAlmostEqual(result["p"], 2 / 64)
        self.assertEqual(result["w_plus"], 21)
        self.assertEqual(result["rank_biserial"], 1.0)
        # Symmetric differences carry no evidence.
        self.assertEqual(bench.wilcoxon([1, -1, 2, -2])["p"], 1.0)
        self.assertEqual(bench.wilcoxon([0, 0, 0])["p"], 1.0)

    def test_hodges_lehmann_interval_uses_the_exact_critical_count(self):
        deltas = [0.8, -0.3, 1.1, 0.4, 2.0, -0.6, 0.9, 1.5, 0.2, 0.7]
        hl = bench.hodges_lehmann(deltas, 0.95)
        walsh = sorted((deltas[i] + deltas[j]) / 2 for i in range(10) for j in range(i, 10))
        # n=10, two-sided 0.05: P(W+ <= 8) = 25/1024 <= 0.025 < P(W+ <= 9), so k = 9.
        self.assertEqual(hl["low"], walsh[8])
        self.assertEqual(hl["high"], walsh[len(walsh) - 9])
        self.assertAlmostEqual(hl["confidence"], 1 - 2 * 25 / 1024)
        self.assertEqual(hl["estimate"], bench.median(walsh))

    def test_no_interval_when_n_cannot_reach_the_confidence(self):
        hl = bench.hodges_lehmann([0.1, 0.2, 0.3, 0.4, 0.5], 0.95)
        self.assertIsNone(hl["low"])
        self.assertEqual(hl["estimate"], 0.3)
        self.assertIsNotNone(bench.hodges_lehmann([0.1] * 6, 0.95)["low"])

    def test_sign_test(self):
        self.assertEqual(bench.sign_test(0, 0), 1.0)
        self.assertAlmostEqual(bench.sign_test(0, 5), 2 / 32)
        self.assertEqual(bench.sign_test(3, 3), 1.0)


def trace_lines(models, turns=4, children=0, extra=()):
    lines = [{"type": "system", "subtype": "init"}, *extra,
             {"type": "result", "subtype": "success", "num_turns": turns, "total_cost_usd": 1.0,
              "subagent_stats": {"spawned": children},
              "modelUsage": {name: {"inputTokens": m[0], "cacheCreationInputTokens": m[1],
                                    "cacheReadInputTokens": m[2], "outputTokens": m[3], "costUSD": m[4]}
                             for name, m in models.items()}}]
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def arm_run(cost, correct=True, trace=None, error=None, delegated=None):
    graders = [{"name": "correct-answer", "passed": correct, "scored": True, "withOnly": False}]
    if delegated is not None:
        graders.insert(0, {"name": "cheap-delegation", "passed": delegated, "scored": False, "withOnly": True})
    return {"score": 1 if correct else 0, "passed": True, "turns": 4, "costUsd": cost, "error": error,
            "tracePath": trace, "graders": graders}


def result(cases):
    return {"schemaVersion": 1, "costUsd": 0, "cases": [
        {"name": name, "arms": {"with": w, "without": wo}} for name, (w, wo) in cases.items()]}


class Parsing(unittest.TestCase):
    def test_usage_sums_every_model_including_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "trace.jsonl")
            path.write_text(trace_lines({"claude-opus-5-5": (8, 1000, 40000, 900, 0.2),
                                         "claude-haiku-5-5": (20, 3000, 9000, 400, 0.01)}, children=1)
                            + "not json\n", encoding="utf-8")
            usage = bench.usage_from_trace(str(path))
        self.assertEqual(usage["children"], 1)
        record = bench.run_record(arm_run(0.21, trace=str(path)), usage)
        self.assertEqual(record["cache_read"], 49000)
        self.assertEqual(record["new_input"], 8 + 1000 + 20 + 3000)
        self.assertEqual(record["output"], 1300)
        self.assertEqual(set(record["models"]), {"claude-opus-5-5", "claude-haiku-5-5"})

    def test_a_trace_without_its_result_record_gives_no_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "trace.jsonl")
            path.write_text(json.dumps({"type": "assistant"}) + "\n", encoding="utf-8")
            self.assertIsNone(bench.usage_from_trace(str(path)))
            self.assertIsNone(bench.usage_from_trace(str(Path(tmp, "missing.jsonl"))))
        record = bench.run_record(arm_run(0.1), None)
        self.assertIsNone(record["cache_read"])
        self.assertEqual(record["cost_usd"], 0.1)

    def test_correctness_reads_only_scored_graders(self):
        self.assertTrue(bench.run_record(arm_run(0.1, delegated=False), None)["correct"])
        self.assertFalse(bench.run_record(arm_run(0.1, correct=False, delegated=True), None)["correct"])
        self.assertEqual(bench.run_record(arm_run(0.1, delegated=True), None)["indicators"], {"cheap-delegation": True})

    def test_runs_pair_by_index_and_partnerless_runs_are_counted(self):
        data = result({"a": ([arm_run(0.3), arm_run(0.2), arm_run(0.1)], [arm_run(0.4), arm_run(0.5)])})
        pairs, unpaired = bench.load_pairs([data])
        self.assertEqual(unpaired, 1)
        self.assertEqual([(p["with"]["cost_usd"], p["without"]["cost_usd"]) for p in pairs], [(0.3, 0.4), (0.2, 0.5)])

    def test_an_errored_run_drops_its_pair_from_every_test(self):
        # As the CLI reports a run it could not start: no cost, every grader failed.
        failed_start = arm_run(0, correct=False, error="Error: ENOENT: no such file or directory, posix_spawn")
        data = result({"a": ([arm_run(0.3), arm_run(0.2)], [failed_start, arm_run(0.5)])})
        summary = bench.report([data])
        self.assertEqual(summary["errored_pairs"], 1)
        self.assertEqual(summary["metrics"][0]["n"], 1)
        # A run that gave no answer is not a wrong answer.
        self.assertEqual(summary["correctness"], {"n": 1, "with": 1, "without": 1, "with_only_correct": 0,
                                                  "without_only_correct": 0, "p": 1.0})
        case = summary["cases"][0]
        self.assertEqual((case["with_correct"], case["without_correct"]), (1, 1))
        self.assertEqual((case["with_cost_median"], case["without_cost_median"]), (0.2, 0.5))
        self.assertTrue(any("errored" in line for line in summary["verdict"]))


class Verdict(unittest.TestCase):
    def summary(self, deltas, base=1.0, with_correct=None):
        runs_with = [arm_run(base + d, correct=True if with_correct is None else with_correct[i])
                     for i, d in enumerate(deltas)]
        runs_without = [arm_run(base) for _ in deltas]
        return bench.report([result({"case": (runs_with, runs_without)})])

    def test_every_claim_states_p_and_n(self):
        for deltas in ([-0.1] * 8, [0.1] * 8, [0.1, -0.1] * 4, [-0.1] * 3):
            with self.subTest(deltas=deltas):
                cost_line = self.summary(deltas)["verdict"][0]
                if "lower" in cost_line or "higher" in cost_line or "significant" in cost_line:
                    self.assertIn("p=", cost_line)
                    self.assertIn(f"n={len(deltas)}", cost_line)

    def test_lower_only_when_significant(self):
        self.assertIn("lower", self.summary([-0.1, -0.2, -0.15, -0.05, -0.3, -0.12, -0.08])["verdict"][0])
        self.assertIn("higher", self.summary([0.1, 0.2, 0.15, 0.05, 0.3, 0.12, 0.08])["verdict"][0])
        mixed = self.summary([-0.1, 0.2, -0.15, 0.05, -0.3, 0.12, 0.08])["verdict"][0]
        self.assertNotIn("lower", mixed)
        self.assertIn("No significant", mixed)

    def test_too_few_pairs_makes_no_claim(self):
        line = self.summary([-0.1, -0.2, -0.3, -0.4, -0.5])["verdict"][0]
        self.assertIn("Too few pairs", line)
        self.assertNotIn("lower", line)

    def test_a_correctness_regression_is_named(self):
        summary = self.summary([0.0] * 7, with_correct=[False] * 7)
        self.assertTrue(any(line.startswith("Correctness regressed") for line in summary["verdict"]))

    def test_render_shows_every_metric(self):
        text = bench.render(self.summary([-0.1] * 7))
        for metric in bench.METRICS:
            self.assertIn(metric, text)


def run_args(**overrides):
    values = dict(runs=3, case=None, tag=None, max_cost_usd=5.0, concurrency=None, ledger=None, budget_usd=None,
                  in_flight_usd=0.5)
    values.update(overrides)
    return SimpleNamespace(**values)


class Running(unittest.TestCase):
    def test_command_runs_both_arms_with_fixtures_and_keeps_the_traces(self):
        cmd = bench.eval_command(run_args(case="bulk-*", concurrency=2), Path("/x/result.json"))
        self.assertEqual(cmd[:4], ["claude", "plugin", "eval", str(ROOT)])
        for flag in ("--scaffold", "--no-publish", "--keep-temp"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--ablation") + 1], "with-without")
        self.assertEqual(cmd[cmd.index("--max-cost-usd") + 1], "5")
        self.assertEqual(cmd[cmd.index("--case") + 1], "bulk-*")
        self.assertNotIn("--allow-tools", cmd)
        self.assertNotIn("--eval-dir", cmd)

    def test_the_write_suite_gets_its_eval_dir_and_only_its_grant(self):
        cmd = bench.eval_command(run_args(eval_dir="evals-write"), Path("/x/result.json"))
        self.assertEqual(cmd[cmd.index("--eval-dir") + 1], "evals-write")
        start = cmd.index("--allow-tools") + 1
        self.assertEqual(tuple(cmd[start:start + len(bench.GRANTS["evals-write"])]), bench.GRANTS["evals-write"])
        self.assertNotIn("Bash", cmd)  # never the whole shell
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            bench.main(["run", "--max-cost-usd", "1", "--eval-dir", "elsewhere"])

    def test_budget_counts_the_ledger_ceiling_and_runs_in_flight(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, "ledger.txt")
            ledger.write_text("# time | command | cost\n2026 | a | 0.50 | x\n2026 | b | 4.25 | y\n2026 | c | unknown\n",
                              encoding="utf-8")
            self.assertAlmostEqual(bench.ledger_total(ledger), 4.75)
            self.assertIsNone(bench.budget_refusal(run_args(ledger=str(ledger), budget_usd=13, max_cost_usd=7,
                                                            concurrency=2)))
            self.assertIn("refusing", bench.budget_refusal(run_args(ledger=str(ledger), budget_usd=13,
                                                                    max_cost_usd=7.5, concurrency=2)))
            self.assertIsNotNone(bench.budget_refusal(run_args(budget_usd=13)))
            self.assertEqual(bench.ledger_total(Path(tmp, "none.txt")), 0.0)

    def test_a_refused_budget_never_starts_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp, "ledger.txt")
            ledger.write_text("t | c | 12.9 | o\n", encoding="utf-8")
            with mock.patch.object(bench.subprocess, "run") as run, mock.patch("sys.stderr"):
                code = bench.main(["run", "--max-cost-usd", "1", "--ledger", str(ledger), "--budget-usd", "13",
                                   "--out", str(Path(tmp, "out"))])
            self.assertEqual(code, 2)
            run.assert_not_called()

    def test_run_records_usage_cleans_kept_dirs_and_appends_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            kept = Path(tmp, "e-Ab12Cd")
            (kept / "out").mkdir(parents=True)
            (kept / "sealed" / "home").mkdir(parents=True)
            trace = kept / "out" / "trace.jsonl"
            trace.write_text(trace_lines({"claude-sonnet-5-5": (6, 100, 2000, 50, 0.03)}), encoding="utf-8")
            os.chmod(kept / "sealed", 0)
            os.chmod(kept, stat.S_IRUSR | stat.S_IXUSR)
            out, ledger = Path(tmp, "out"), Path(tmp, "ledger.txt")
            data = result({"c": ([arm_run(0.03, trace=str(trace))], [arm_run(0.02)])})
            data["costUsd"] = 0.05

            def fake_eval(cmd, **kwargs):
                Path(cmd[cmd.index("--json") + 1]).write_text(json.dumps(data), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with mock.patch.object(bench.subprocess, "run", side_effect=fake_eval), \
                    mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                code = bench.main(["run", "--max-cost-usd", "1", "--out", str(out), "--ledger", str(ledger)])
            self.assertEqual(code, 0)
            self.assertFalse(kept.exists())
            usage = json.loads((out / "usage.json").read_text(encoding="utf-8"))
            self.assertEqual(usage[str(trace)]["models"]["claude-sonnet-5-5"]["cache_read"], 2000)
            self.assertAlmostEqual(bench.ledger_total(ledger), 0.05)
            self.assertTrue((out / "summary.json").is_file())

    def test_cleanup_refuses_anything_but_a_kept_run_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel in ("other/out/trace.jsonl", "e-Ab12Cd/trace.jsonl", "e-Ab12Cd/out/notes.txt"):
                path = Path(tmp, rel)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
                with self.subTest(path=rel):
                    self.assertFalse(bench.remove_kept(str(path)))
                    self.assertTrue(path.exists())
            target = Path(tmp, "elsewhere")
            target.mkdir()
            link = Path(tmp, "e-Zz99Yy")
            link.symlink_to(target)
            (target / "out").mkdir()
            (target / "out" / "trace.jsonl").write_text("x", encoding="utf-8")
            self.assertFalse(bench.remove_kept(str(link / "out" / "trace.jsonl")))
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
