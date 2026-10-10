"""The dispatch report: joins, coverage, counts and reference cost, from rows shaped as the hooks write them."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dispatch_log

# One catalog row in the shape pricing.snapshot keeps; per-token USD.
CATALOG = {"schema": 1, "source": "fixture", "fetched_at": 0, "models": [
    {"id": "anthropic/claude-haiku-4.5", "pricing": {"prompt": "0.000001", "completion": "0.000005",
                                                    "input_cache_read": "0.0000001"}}]}


def dispatch(ts, agent, decision="allow", call=None, session="s", harness="claude", **extra):
    """A dispatch row as dispatch_log.record() and dispatch_guard.process() write it."""
    row = {"v": 3, "ts": ts, "harness": harness, "session": session, "decision": decision, "reason": "x",
           "tool": "Agent", "agent": agent, "model": None, "tier": dispatch_log._tier_of({"agent": agent}),
           "escalation_from": None, "call_id": call}
    row.update(extra)
    return row


def child(ts, agent=None, agent_id=None, call=None, session="s", harness="claude", outcome="done", verified=True, **extra):
    """A completion row as observe_agent.observe() writes it."""
    row = {"v": 3, "ts": ts, "harness": harness, "session": session, "decision": "completed", "agent": agent,
           "agent_id": agent_id, "call_id": call, "outcome": outcome, "verified": verified, "outcome_source": "message",
           "usage": None, "usage_complete": None, "turns": None, "effective_model": None}
    row.update(extra)
    return row


class Joins(unittest.TestCase):
    def test_an_untiered_completion_cannot_take_a_tiered_dispatch(self):
        summary = dispatch_log.summarise([
            dispatch("2026-10-01T10:00:00Z", "general-purpose", decision="correct"),
            dispatch("2026-10-01T10:00:05Z", "leos-agent:leo-cheap", decision="correct"),
            child("2026-10-01T10:00:10Z", "general-purpose", "a1", outcome="blocked", verified=None),
            child("2026-10-01T10:00:20Z", "leos-agent:leo-cheap", "a2"),
        ])
        self.assertEqual(summary["outcomes"], {"cheap": {"done": 1}, "unrouted": {"blocked": 1}})
        self.assertEqual(summary["joins"], {"nearest": 2})

    def test_siblings_of_one_agent_finishing_together_stay_distinct(self):
        rows = [dispatch("2026-10-01T10:00:00Z", "delegate_task", harness="hermes", tier=None)]
        rows += [child("2026-10-01T10:00:0%dZ" % n, "delegate_task", "child-%d" % n, harness="hermes", outcome=o, verified=False)
                 for n, o in enumerate(("done", "blocked", "partial"))]
        summary = dispatch_log.summarise(rows)
        self.assertEqual(summary["outcomes"], {"unrouted": {"done": 1, "blocked": 1, "partial": 1}})

    def test_reports_of_one_child_merge_and_keep_a_known_outcome(self):
        summary = dispatch_log.summarise([
            dispatch("2026-10-01T10:00:00Z", "leo-cheap"),
            dispatch("2026-10-01T10:00:30Z", "leo-cheap"),
            child("2026-10-01T10:00:10Z", "leo-cheap", "a1", outcome="done"),
            # Its SessionEnd backfill keeps the child's own stop time and adds the model.
            child("2026-10-01T10:00:10Z", "leo-cheap", "a1", outcome="unknown", decision="executed", effective_model="haiku"),
            child("2026-10-01T10:00:40Z", "leo-cheap", "a2", outcome="partial"),
        ])
        self.assertEqual(summary["outcomes"], {"cheap": {"done": 1, "partial": 1}})
        self.assertEqual(summary["joins"], {"nearest": 2})
        self.assertEqual(summary["superseded"], 1)
        self.assertEqual(summary["confirmed_executions"], 1)

    def test_children_sharing_a_call_id_are_one_batched_dispatch(self):
        summary = dispatch_log.summarise([
            dispatch("2026-10-01T10:00:00Z", "delegate_task", harness="hermes", tier=None, call="other"),
            dispatch("2026-10-01T10:01:00Z", "delegate_task", harness="hermes", tier=None, call="c1"),
            child("2026-10-01T10:02:00Z", "delegate_task", "c1#0", call="c1", harness="hermes"),
            child("2026-10-01T10:02:00Z", "delegate_task", "c1#1", call="c1", harness="hermes", outcome="blocked", verified=False),
        ])
        self.assertEqual(summary["joins"], {"call_id": 2})
        self.assertEqual(summary["coverage"], {"unrouted": {"ran": 2, "no_signal": 1}})

    def test_a_completion_whose_call_id_is_missing_does_not_guess(self):
        summary = dispatch_log.summarise([
            dispatch("2026-10-01T10:00:00Z", "leo-cheap", harness="opencode", call="c1"),
            child("2026-10-01T10:01:00Z", "leo-cheap", call="rotated-away", harness="opencode"),
        ])
        self.assertEqual(summary["joins"], {"unmatched": 1})
        self.assertEqual(summary["coverage"], {"cheap": {"ran": 1, "no_signal": 1}})


class Counts(unittest.TestCase):
    def test_a_block_and_its_retry_are_one_escalation_and_one_tier_dispatch(self):
        summary = dispatch_log.summarise([
            dispatch("2026-10-01T10:00:00Z", "leo-standard", decision="block", escalation_from="cheap"),
            dispatch("2026-10-01T10:00:01Z", "leo-standard", escalation_from="cheap"),
            dispatch("2026-10-01T10:01:00Z", "spawn_agent", decision="block", harness="codex", tier=None, escalation_from="unobservable"),
            dispatch("2026-10-01T10:01:01Z", "spawn_agent", harness="codex", tier=None, escalation_from="unobservable", model="m"),
        ])
        self.assertEqual(summary["escalations"], {"cheap->standard": 1})
        self.assertEqual(summary["escalation_unobservable"], 1)
        self.assertEqual(summary["tiers"], {"leo-standard": 1, "explicit model": 1})
        self.assertEqual(summary["blocked"], 2)

    def test_dispatches_without_a_completion_signal_are_counted_beside_the_outcomes(self):
        summary = dispatch_log.summarise([
            {"decision": "allow", "agent": "leo-cheap", "session": "s"},  # predates instrumentation
            dispatch("2026-10-01T10:00:00Z", "leo-cheap"),
            dispatch("2026-10-01T10:00:01Z", "leo-cheap"),
            dispatch("2026-10-01T10:00:02Z", "leo-cheap", decision="block"),
            child("2026-10-01T10:00:30Z", "leo-cheap", "a1"),
        ])
        self.assertEqual(summary["coverage"], {"cheap": {"ran": 2, "no_signal": 1}})
        self.assertEqual(summary["outcomes"], {"cheap": {"done": 1}})
        self.assertIn("cheap     done 1  | no completion signal 1", dispatch_log.render(summary))

    def test_diagnostic_tokens_are_counted_and_never_echoed(self):
        summary = dispatch_log.summarise([
            dispatch("2026-10-01T10:00:00Z", "leo-cheap", diagnostic="routing-config-invalid"),
            dispatch("2026-10-01T10:00:01Z", "leo-cheap", decision="block", diagnostic="routing-config-invalid"),
            dispatch("2026-10-01T10:00:02Z", "leo-cheap", diagnostic="PRIVATE free text /home/leo"),
            dispatch("2026-10-01T10:00:03Z", "leo-cheap"),
        ])
        self.assertEqual(summary["diagnostics"], {"other": 1, "routing-config-invalid": 2})
        self.assertIn("diagnostics other 1, routing-config-invalid 2", dispatch_log.render(summary))
        self.assertNotIn("PRIVATE", json.dumps(summary))

    def test_reasons_and_error_types_group_by_decision_and_never_echo_free_text(self):
        # The guard's reason enums, its newer error rows (error_type plus a
        # reason token), and an older error row whose reason was exception text.
        rows = [dispatch("2026-10-01T10:00:0%dZ" % n, "leo-cheap", reason=reason)
                for n, reason in enumerate(("agent-defined-model", "inherits-parent", "inherits-parent",
                                            "fork-inherits-parent", "parent-model-unavailable", "a-reason-added-later"))]
        rows += [dispatch("2026-10-01T10:00:06Z", "leo-cheap", decision="block", reason="over-ceiling", diagnostic="unrecognized-guard-mode"),
                 {"v": 3, "ts": "2026-10-01T10:00:07Z", "harness": "claude", "decision": "error", "reason": "guard-error", "error_type": "KeyError"},
                 {"v": 3, "ts": "2026-10-01T10:00:08Z", "harness": "codex", "decision": "error", "reason": "invalid-hook-input", "error_type": "ValueError"},
                 {"v": 3, "ts": "2026-10-01T10:00:09Z", "harness": "codex", "decision": "error", "reason": "ValueError: PRIVATE /home/leo/x",
                  "error_type": "not a class name PRIVATE"},
                 {"v": 3, "ts": "2026-10-01T10:00:10Z", "harness": "codex", "decision": "error"}]
        summary = dispatch_log.summarise(rows)
        self.assertEqual(summary["reasons"], {
            "allow": {"a-reason-added-later": 1, "agent-defined-model": 1, "fork-inherits-parent": 1,
                      "inherits-parent": 2, "parent-model-unavailable": 1},
            "block": {"over-ceiling": 1},
            "error": {"guard-error": 1, "invalid-hook-input": 1, "none": 1, "other": 1}})
        self.assertEqual(summary["error_types"], {"KeyError": 1, "ValueError": 1, "other": 1})
        self.assertEqual(summary["diagnostics"], {"unrecognized-guard-mode": 1})
        self.assertEqual(summary["errors"], 4)
        text = dispatch_log.render(summary)
        self.assertIn("reasons     allow: inherits-parent 2, a-reason-added-later 1", text)
        self.assertIn("error: guard-error 1, invalid-hook-input 1, none 1, other 1", text)
        self.assertIn("error types KeyError 1, ValueError 1, other 1", text)
        self.assertNotIn("PRIVATE", text + json.dumps(summary))


class Cost(unittest.TestCase):
    def test_reference_cost_per_verified_success_and_turns_per_tier(self):
        summary = dispatch_log.summarise([
            dispatch("2026-10-01T10:00:00Z", "leo-cheap", call="a"),
            dispatch("2026-10-01T10:01:00Z", "leo-cheap", call="b"),
            dispatch("2026-10-01T10:02:00Z", "leo-cheap", call="c"),
            child("2026-10-01T10:00:30Z", "leo-cheap", call="a", decision="executed", effective_model="claude-haiku-4-5",
                  usage={"input": 1000, "output": 200, "cache_read": 10000}, turns=3),
            child("2026-10-01T10:01:30Z", "leo-cheap", call="b", decision="executed", effective_model="claude-haiku-4-5",
                  outcome="blocked", verified=False, usage={"input": 1000, "output": 200}, turns=5),
            child("2026-10-01T10:02:30Z", "leo-cheap", call="c", decision="executed", effective_model="private-local-model",
                  usage={"input": 5, "output": 5}),
        ], catalog=CATALOG)
        cost = summary["cost"]["cheap"]
        # (1000*1e-6 + 200*5e-6 + 10000*1e-7) + (1000*1e-6 + 200*5e-6) = 0.003 + 0.002
        self.assertEqual((cost["priced_runs"], cost["unpriced_runs"], cost["verified_successes"]), (2, 1, 1))
        self.assertAlmostEqual(cost["reference_usd"][0], 0.005)
        self.assertAlmostEqual(cost["per_verified_success_usd"][1], 0.005)
        self.assertEqual(summary["turns"], {"cheap": {"total": 8, "rows": 2}})
        text = dispatch_log.render(summary)
        self.assertIn("cheap $0.0050 per verified success ($0.0050 over 2 priced runs, 1 verified), 1 unpriced", text)
        self.assertIn("1 unpriced", text)
        self.assertIn("not a bill", text)
        self.assertIn("turns       cheap 8 over 2 runs (mean 4.0)", text)

    def test_no_usage_means_no_cost_section_and_no_catalog_read(self):
        with mock.patch("pricing.load", side_effect=AssertionError("must not load")):
            summary = dispatch_log.summarise([dispatch("2026-10-01T10:00:00Z", "leo-cheap"),
                                              child("2026-10-01T10:00:30Z", "leo-cheap", "a1")])
        self.assertEqual(summary["cost"], {})
        self.assertNotIn("  cost  ", dispatch_log.render(summary))


LENS = "leos-agent:leo-lens"


class LensTiers(unittest.TestCase):
    """A Claude leo-lens is logged under the tier of the model it runs on."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": tmp.name})
        env.start(); self.addCleanup(env.stop)
        import routing_engine
        self.run_tier = routing_engine.run_tier

    def test_the_cheap_family_is_cheap_whether_named_by_alias_or_by_id(self):
        # The Agent call carries the alias; transcripts, a capped parent and
        # provider routes carry full IDs.
        for model in ("haiku", "claude-haiku-5-5", "claude-haiku-4-5-20251001",
                      "us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5@20251001"):
            with self.subTest(model=model):
                self.assertEqual(self.run_tier(LENS, "claude", model), "cheap")
                self.assertEqual(self.run_tier("leo-lens", "claude", model), "cheap")
        for model in ("sonnet", "claude-sonnet-5-5", "opus", "claude-opus-5-5[1m]", "inherit", "private-local-model", "", None):
            with self.subTest(model=model):
                self.assertEqual(self.run_tier(LENS, "claude", model), "standard")

    def test_the_configured_cheap_tier_decides_not_a_model_name(self):
        (self.data / "routing.json").write_text(json.dumps({"claude": {"cheap": "sonnet", "standard": "opus"}}))
        self.assertEqual(self.run_tier(LENS, "claude", "claude-sonnet-5-5"), "cheap")
        self.assertEqual(self.run_tier(LENS, "claude", "haiku"), "standard")
        # When cheap and standard name one family the model carries no tier.
        same = {"claude": {"standard": {"model": "haiku", "effort": None}}}
        self.assertEqual(self.run_tier(LENS, "claude", "haiku", same), "standard")
        # A routing file that fails validation falls back to the defaults, as the guard does.
        (self.data / "routing.json").write_text("{not json")
        self.assertEqual(self.run_tier(LENS, "claude", "haiku"), "cheap")

    def test_other_profiles_and_other_harnesses_keep_their_own_tier(self):
        for agent, tier in (("leos-agent:leo-cheap", "cheap"), ("leo-standard", "standard"), ("leo-reviewer", "standard"),
                            ("leo-premium", "premium"), ("general-purpose", None), ("other-plugin:leo-lens", None)):
            with self.subTest(agent=agent):
                self.assertEqual(self.run_tier(agent, "claude", "haiku"), tier)
                self.assertEqual(self.run_tier(agent, "claude", "opus"), tier)
        # Codex, OpenCode and Cursor hold a lens to standard; a cheap lens there is leo-cheap.
        for harness, model in (("codex", "gpt-6-luna"), ("opencode", "anthropic/claude-haiku-4-5"),
                               ("cursor", "claude-haiku-4-5"), ("hermes", "haiku"), ("pi", "haiku")):
            with self.subTest(harness=harness):
                self.assertEqual(self.run_tier("leo-lens", harness, model), "standard")

    def test_cheap_and_standard_lenses_launched_together_pair_at_their_own_tier(self):
        # As the guard and the observer now write them, with no link rows: both
        # dispatches share one second, so one tier could only call it a tie.
        rows = [dispatch("2026-10-01T10:00:00Z", LENS, decision="correct", tier="cheap", effective_model="haiku"),
                dispatch("2026-10-01T10:00:00Z", LENS, decision="correct", tier="standard", effective_model="sonnet"),
                child("2026-10-01T10:00:30Z", LENS, "a1", decision="executed", tier="cheap", effective_model="claude-haiku-5-5"),
                child("2026-10-01T10:00:40Z", LENS, "a2", decision="executed", tier="standard",
                      effective_model="claude-sonnet-5-5", outcome="partial")]
        summary = dispatch_log.summarise(rows)
        self.assertEqual(summary["outcomes"], {"cheap": {"done": 1}, "standard": {"partial": 1}})
        self.assertEqual(summary["joins"], {"nearest": 2})
        self.assertEqual(summary["coverage"], {"cheap": {"ran": 1, "no_signal": 0}, "standard": {"ran": 1, "no_signal": 0}})

    def test_rows_written_before_lens_tiers_report_as_they_always_did(self):
        # A lens dispatch logged standard whatever its model, and a lens
        # completion carried no tier, so both read as standard.
        rows = [dispatch("2026-10-01T10:00:00Z", LENS, decision="correct", effective_model="haiku"),
                dispatch("2026-10-01T10:00:00Z", LENS, decision="correct", effective_model="sonnet"),
                child("2026-10-01T10:00:30Z", LENS, "a1", decision="executed", effective_model="claude-haiku-5-5"),
                child("2026-10-01T10:00:40Z", LENS, "a2", decision="executed", effective_model="claude-sonnet-5-5")]
        self.assertEqual(rows[0]["tier"], "standard")
        summary = dispatch_log.summarise(rows)
        self.assertEqual(summary["outcomes"], {"ambiguous": {"done": 1}, "standard": {"done": 1}})
        self.assertEqual(summary["joins"], {"ambiguous": 1, "nearest": 1})
        self.assertEqual(summary["contract_refused"], {})


class HandbackRefusals(unittest.TestCase):
    def test_refusals_are_counted_per_tier_beside_the_outcomes_never_in_them(self):
        summary = dispatch_log.summarise([
            dispatch("2026-10-01T10:00:00Z", "leo-cheap", call="a"),
            dispatch("2026-10-01T10:00:01Z", "leo-cheap", call="b"),
            dispatch("2026-10-01T10:00:02Z", "leo-standard", call="c"),
            child("2026-10-01T10:00:30Z", "leo-cheap", "a1", call="a", contract_refused=True),
            # Its SessionEnd backfill repeats the flag; the child is counted once.
            child("2026-10-01T10:00:30Z", "leo-cheap", "a1", call="a", decision="executed", contract_refused=True,
                  effective_model="haiku"),
            child("2026-10-01T10:00:31Z", "leo-cheap", "b1", call="b", contract_refused=False),
            child("2026-10-01T10:00:32Z", "leo-standard", "c1", call="c", outcome="unknown", verified=None,
                  contract_refused=True),
        ])
        self.assertEqual(summary["contract_refused"], {"cheap": 1, "standard": 1})
        self.assertEqual(summary["outcomes"], {"cheap": {"done": 2}, "standard": {"unknown": 1}})
        self.assertEqual(summary["verified"], {"cheap": {"stated": 2}, "standard": {"unstated": 1}})
        text = dispatch_log.render(summary)
        self.assertIn("cheap     done 2  | hand-back refused 1", text)
        self.assertIn("standard  unknown 1  | hand-back refused 1", text)

    def test_rows_without_the_flag_print_no_refusals(self):
        summary = dispatch_log.summarise([dispatch("2026-10-01T10:00:00Z", "leo-cheap", call="a"),
                                          child("2026-10-01T10:00:30Z", "leo-cheap", "a1", call="a")])
        self.assertEqual(summary["contract_refused"], {})
        self.assertNotIn("refused", dispatch_log.render(summary))


class RotationSafeRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": self.tmp.name})
        env.start(); self.addCleanup(env.stop)

    def test_a_rotation_between_the_two_generations_drops_nothing(self):
        current = dispatch_log.path()
        with open(current + ".1", "w") as fh:
            fh.write(json.dumps({"n": "old"}) + "\n")
        with open(current, "w") as fh:
            fh.write(json.dumps({"n": "kept"}) + "\n")
        real, fired = dispatch_log._lines, []

        def racing(candidate):
            lines = real(candidate)
            if candidate.endswith(".1") and not fired:
                fired.append(True)
                # append() rotates exactly like this, under its own lock.
                with mock.patch.object(dispatch_log, "MAX_BYTES", 0):
                    dispatch_log.append({"n": "new"})
            return lines

        with mock.patch.object(dispatch_log, "_lines", side_effect=racing):
            rows = dispatch_log.read()
        self.assertEqual([r["n"] for r in rows], ["kept", "new"])


if __name__ == "__main__":
    unittest.main()
