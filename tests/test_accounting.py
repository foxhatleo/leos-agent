"""One accounting path: the usage scan and the dispatch report count and price a child alike.

The rules live once, in scripts/accounting.py. The equality tests below feed
the same transcript, or the same tokens, through each caller's own entry
point, so a caller that stops using a shared rule, or reads its records
differently, fails here rather than in two reports that quietly disagree.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import accounting  # noqa: E402
import dispatch_log  # noqa: E402
import pricing  # noqa: E402
import session_models  # noqa: E402
import usage_scan  # noqa: E402

STAMP = "2026-10-01T10:00:00Z"
FIELDS = accounting.TOKEN_KEYS
OVERRIDES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "PI_CODING_AGENT_DIR", "OPENCODE_CONFIG_DIR",
             "OPENCODE_CONFIG", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "OPENCODE_DB")
# Per-token USD in the shape pricing.snapshot keeps: one plain rate card, one
# with a long-prompt conditional row, one with no cache rates at all.
CATALOG = {"schema": 1, "source": "fixture", "fetched_at": 0, "models": [
    {"id": "anthropic/claude-haiku-4.5", "pricing": {"prompt": "0.000001", "completion": "0.000005",
                                                    "input_cache_read": "0.0000001", "input_cache_write": "0.00000125"}},
    {"id": "anthropic/claude-sonnet-5", "pricing": {"prompt": "0.000003", "completion": "0.000015",
                                                   "input_cache_read": "0.0000003", "input_cache_write": "0.00000375",
                                                   "overrides": [{"min_prompt_tokens": 200000, "prompt": "0.000006",
                                                                  "completion": "0.0000225"}]}},
    {"id": "openai/gpt-5.6-luna", "pricing": {"prompt": "0.0000005", "completion": "0.000004"}},
]}


def claude_record(request, message, usage, **extra):
    record = {"type": "assistant", "timestamp": STAMP, "sessionId": "s",
              "message": {"model": "haiku", "content": [], "usage": usage, **({"id": message} if message else {})}}
    if request:
        record["requestId"] = request
    record.update(extra)
    return record


def codex_event(total=None, last=None, info=True):
    payload = {"type": "token_count", "info": None}
    if info:
        payload["info"] = {"total_token_usage": total, "last_token_usage": last} if total is not None else {"last_token_usage": last}
    return {"type": "event_msg", "timestamp": STAMP, "payload": payload}


def tokens(inp, cached, out, write=0):
    return {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": write, "output_tokens": out}


class Sandboxed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"HOME": str(self.root / "home"), "LEOS_AGENT_LOCAL_PATH": str(self.root / "local"),
                                           "LEOS_AGENT_PRICE_REFRESH": "off",
                                           **{key: str(self.root / key.lower()) for key in OVERRIDES}})
        env.start()
        self.addCleanup(env.stop)

    def write(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path


class SharedRules(unittest.TestCase):
    def test_claude_snapshots_of_one_request_keep_each_categorys_largest(self):
        first = accounting.claude_usage({"usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 500}})
        final = accounting.claude_usage({"usage": {"input_tokens": 10, "output_tokens": 850}})
        self.assertEqual(accounting.keep_largest(accounting.keep_largest(None, first), final), (10, 500, 0, 850))
        self.assertIsNone(accounting.claude_usage({"usage": {}}))
        self.assertEqual(accounting.claude_usage({"usage": {"input_tokens": True, "output_tokens": -4}}), (0, 0, 0, 0))

    def test_a_claude_request_is_its_request_id_then_its_message_id(self):
        self.assertEqual(accounting.claude_key({"requestId": "r"}, {"id": "m"}, 7), "r")
        self.assertEqual(accounting.claude_key({}, {"id": "m"}, 7), "m")
        self.assertEqual(accounting.claude_key({}, {}, 7), 7)

    def test_codex_counts_each_cumulative_change_once_with_cache_out_of_input(self):
        deltas = accounting.CodexDeltas()
        steps = [deltas.step(codex_event(tokens(100, 60, 9), tokens(100, 60, 9))["payload"]),
                 deltas.step(codex_event(tokens(100, 60, 9), tokens(100, 60, 9))["payload"]),  # written again
                 deltas.step(codex_event(tokens(250, 200, 30), tokens(150, 140, 21))["payload"])]
        self.assertEqual(steps, [((100, 60, 0, 9), None), (None, None), ((150, 140, 0, 21), None)])
        self.assertEqual([accounting.codex_request(d) for d, _ in steps if d], [(40, 60, 0, 9), (10, 140, 0, 21)])
        self.assertEqual(accounting.codex_request((5, 9, 0, 1)), (0, 9, 0, 1))  # never negative

    def test_codex_gaps_are_named_and_rate_limit_events_are_not_gaps(self):
        deltas = accounting.CodexDeltas()
        self.assertEqual(deltas.step(codex_event(info=False)["payload"]), (None, None))
        self.assertEqual(deltas.step({"type": "token_count"}), (None, None))
        self.assertEqual(deltas.step(codex_event(last=tokens(1, 0, 1))["payload"]), (None, accounting.MISSING_TOTAL))
        self.assertEqual(deltas.step(codex_event(tokens(50, 0, 5))["payload"]), (None, accounting.MISSING_BASELINE))
        self.assertEqual(deltas.step(codex_event(tokens(80, 0, 8))["payload"]), ((30, 0, 0, 3), None))
        # A total that falls is a reset: the event's own last usage stands in.
        self.assertEqual(deltas.step(codex_event(tokens(20, 0, 2), tokens(20, 0, 2))["payload"]), ((20, 0, 0, 2), accounting.RESET))
        self.assertEqual(deltas.step({"type": "token_count", "info": ["not", "a", "dict"]}), (None, accounting.MISSING_TOTAL))

    def test_reference_range_prices_conditional_rows_as_a_range_and_keeps_gaps_explicit(self):
        sonnet = pricing.resolve("claude-sonnet-5", CATALOG)
        low, high, unpriced = accounting.reference_range(sonnet, {"input": 1000, "output": 100})
        self.assertEqual((float(low), float(high), unpriced), (0.0045, 0.00825, 0))
        luna = pricing.resolve("gpt-5.6-luna", CATALOG)
        low, high, unpriced = accounting.reference_range(luna, {"input": 1000, "cache_read": 2000, "output": 10})
        self.assertEqual((float(low), float(high), unpriced), (0.00054, 0.00054, 2000))
        unknown = pricing.resolve("private-local-model", CATALOG)
        self.assertEqual(accounting.reference_range(unknown, {"input": 5, "output": 5})[2], 10)


class CallersAgree(Sandboxed):
    """The drift test, kept as equality: the same records through both callers."""

    def assert_same_usage(self, stats, scanned):
        self.assertEqual({k: stats["usage"].get(k, 0) for k in FIELDS}, {k: scanned[k] for k in FIELDS})
        self.assertEqual(stats["turns"], scanned["requests"])

    def test_claude_child_usage_agrees_with_the_usage_scan(self):
        child = self.write(self.root / "claude" / "proj" / "sess" / "subagents" / "agent-k.jsonl", [
            # A later snapshot that omits a category keeps the earlier count.
            claude_record("r1", "m1", {"input_tokens": 4, "output_tokens": 1, "cache_creation_input_tokens": 90}),
            claude_record("r1", "m1", {"input_tokens": 4, "output_tokens": 60}),
            claude_record(None, "m2", {"input_tokens": 7, "cache_read_input_tokens": 300, "output_tokens": 2}),
            claude_record(None, "m2", {"input_tokens": 7, "cache_read_input_tokens": 300, "output_tokens": 11}),
            claude_record(None, None, {"input_tokens": 1, "output_tokens": 1}),
            {"type": "user", "timestamp": STAMP, "message": {"content": "brief"}},
        ])
        scanned = usage_scan.scan_claude(0, str(self.root / "claude"))["model_usage"]["haiku"]["subagent"]
        stats = session_models.transcript_stats(str(child))
        self.assert_same_usage(stats, scanned)
        self.assertEqual(stats["usage"], {"input": 12, "cache_read": 300, "cache_write": 90, "output": 72})

    def test_codex_child_usage_agrees_with_the_usage_scan(self):
        rollout = self.write(self.root / "codex" / "sessions" / "rollout-kid.jsonl", [
            {"type": "session_meta", "timestamp": STAMP, "payload": {"source": {"subagent": {}}}},
            {"type": "turn_context", "timestamp": STAMP, "payload": {"model": "gpt-5.6-luna"}},
            codex_event(info=False),  # rate limits before the first response
            codex_event(tokens(100, 60, 9), tokens(100, 60, 9)),
            codex_event(tokens(100, 60, 9), tokens(100, 60, 9)),
            codex_event(tokens(250, 200, 30), tokens(150, 140, 21)),
            codex_event(tokens(40, 10, 4), tokens(40, 10, 4)),  # a reset
        ])
        scan = usage_scan.scan_codex(0, str(self.root / "codex" / "sessions"))
        stats = session_models.transcript_stats(str(rollout))
        self.assert_same_usage(stats, scan["model_usage"]["gpt-5.6-luna"]["subagent"])
        self.assertEqual(stats["usage"], {"input": 80, "cache_read": 210, "output": 34})
        self.assertIs(stats["complete"], True)
        self.assertEqual(scan["diagnostics"], {"cumulative_resets": 1})

    def test_an_unestablished_codex_request_is_a_gap_to_both(self):
        rollout = self.write(self.root / "codex" / "sessions" / "rollout-gap.jsonl", [
            {"type": "turn_context", "timestamp": STAMP, "payload": {"model": "gpt-5.6-luna"}},
            codex_event(tokens(100, 60, 9)),
            codex_event(tokens(130, 60, 12), tokens(30, 0, 3)),
        ])
        scan = usage_scan.scan_codex(0, str(self.root / "codex" / "sessions"))
        stats = session_models.transcript_stats(str(rollout))
        self.assert_same_usage(stats, scan["model_usage"]["gpt-5.6-luna"]["main"])
        self.assertIs(stats["complete"], False)
        self.assertEqual(scan["diagnostics"], {"missing_initial_delta": 1})

    def test_a_childs_reference_cost_is_the_usage_scans(self):
        cases = [("claude-haiku-4-5", {"input": 1000, "output": 200, "cache_read": 10000, "cache_write": 50}),
                 ("claude-sonnet-5", {"input": 3000, "output": 700}),
                 ("claude-sonnet-5", {"input": 3000, "output": 700, "cache_read": 900}),
                 ("gpt-5.6-luna", {"input": 400, "output": 40}),
                 ("gpt-5.6-luna", {"input": 400, "output": 40, "cache_read": 4000}),
                 ("private-local-model", {"input": 5, "output": 5})]
        for model, usage in cases:
            with self.subTest(model=model, usage=usage):
                scanned = usage_scan.reference_cost(model, {"subagent": {k: usage.get(k, 0) for k in FIELDS}}, CATALOG)
                reported = dispatch_log._reference_usd(model, usage, CATALOG)
                if scanned["unpriced_tokens"]:
                    self.assertIsNone(reported)  # the report prices a child whole or not at all
                else:
                    self.assertEqual(reported, (scanned["minimum_usd"], scanned["maximum_usd"]))

    def test_the_dispatch_report_totals_what_the_scan_would_charge(self):
        usage = {"input": 1000, "output": 200, "cache_read": 10000}
        summary = dispatch_log.summarise([
            {"v": 3, "ts": "2026-10-01T10:00:00Z", "harness": "claude", "session": "s", "decision": "allow",
             "reason": "x", "tool": "Agent", "agent": "leo-cheap", "tier": "cheap", "call_id": "a"},
            {"v": 3, "ts": "2026-10-01T10:00:30Z", "harness": "claude", "session": "s", "decision": "executed",
             "agent": "leo-cheap", "call_id": "a", "outcome": "done", "verified": True,
             "effective_model": "claude-haiku-4-5", "usage": usage}], catalog=CATALOG)
        scanned = usage_scan.reference_cost("claude-haiku-4-5", {"subagent": {k: usage.get(k, 0) for k in FIELDS}}, CATALOG)
        self.assertEqual(summary["cost"]["cheap"]["reference_usd"], [scanned["minimum_usd"], scanned["maximum_usd"]])


class HookPathStaysLight(unittest.TestCase):
    def test_hook_path_modules_load_no_catalog_or_scanner(self):
        """The dispatch guard loads session_models and dispatch_log, and every
        hook that writes a row loads dispatch_log; sharing the rules must not
        pull the price catalog, decimal arithmetic or the usage scanner in."""
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "import accounting, dispatch_guard, dispatch_log, handback_contract, link_agent, outcome, session_models; "
                 "print(sorted(m for m in ('decimal', 'pricing', 'routing_engine', 'settings_probe', 'usage_scan') if m in sys.modules))")
        done = subprocess.run([sys.executable, "-c", probe, str(ROOT / "scripts")], capture_output=True, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=30)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "[]"), done.stderr)


if __name__ == "__main__":
    unittest.main()
