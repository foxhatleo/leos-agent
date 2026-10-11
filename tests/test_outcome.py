"""Fixed tokens in, fixed tokens out: no reply or brief text survives parsing."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import outcome


class Parse(unittest.TestCase):
    def test_every_outcome_round_trips(self):
        for token in outcome.OUTCOMES:
            self.assertEqual(outcome.parse("work\nResult: %s\nVerified: ran tests" % token)["outcome"], token)

    def test_markdown_furniture_is_tolerated(self):
        for line in ("**Result:** done", "`Result: done`", "> Result: done.", "- Result:  DONE   ", "## Result - done"):
            self.assertEqual(outcome.parse("blah\n" + line)["outcome"], "done", line)
        self.assertIs(outcome.parse("**Result:** done\n**Verified:** `unittest` green")["verified"], True)

    def test_the_closing_line_beats_an_earlier_quotation(self):
        text = "I will end with `Result: done` as required.\n...\nResult: escalate\nVerified: none"
        self.assertEqual(outcome.parse(text)["outcome"], "escalate")

    def test_missing_or_foreign_tokens_are_unknown_not_echoed(self):
        self.assertEqual(outcome.parse("all good, bye")["outcome"], "unknown")
        parsed = outcome.parse("Result: SECRET_TOKEN_XYZ")
        self.assertEqual(parsed["outcome"], "unknown")
        self.assertNotIn("SECRET", repr(parsed))
        self.assertEqual(outcome.parse(None), {"outcome": "unknown", "verified": None})
        self.assertEqual(outcome.parse(""), {"outcome": "unknown", "verified": None})

    def test_verified_is_tri_state_and_never_carries_evidence(self):
        self.assertIs(outcome.parse("Result: done\nVerified: none")["verified"], False)
        self.assertIs(outcome.parse("Result: done\nVerified: N/A.")["verified"], False)
        self.assertIs(outcome.parse("Result: done\nVerified:")["verified"], False)
        parsed = outcome.parse("Result: done\nVerified: ran `pytest tests/test_private_thing.py`")
        self.assertIs(parsed["verified"], True)
        self.assertNotIn("private", repr(parsed))
        self.assertIsNone(outcome.parse("Result: done")["verified"])

    def test_dashes_and_equals_separate_a_label_from_its_value(self):
        for sep in ("—", "–", "-", "=", ":"):
            parsed = outcome.parse("Result %s done\nVerified %s ran pytest" % (sep, sep))
            self.assertEqual(parsed, {"outcome": "done", "verified": True}, sep)
        self.assertEqual(outcome.parse("**Result** — partial")["outcome"], "partial")

    def test_an_empty_verified_line_takes_its_evidence_from_the_next_line(self):
        self.assertIs(outcome.parse("Result: done\nVerified:\n`pytest` 3 passed")["verified"], True)
        self.assertIs(outcome.parse("Result: done\nVerified:\n\n- none, docs only")["verified"], False)
        self.assertIs(outcome.parse("Verified:\nResult: done")["verified"], False)
        parsed = outcome.parse("Result: done\n**Verified:**\n- ran `make private-target`")
        self.assertIs(parsed["verified"], True)
        self.assertNotIn("private", repr(parsed))

    def test_no_evidence_is_decided_by_the_first_word(self):
        for value in ("none (read-only task)", "None - no tests exist", "not run", "N/A: docs only", "nothing to run",
                      "untested", "skipped, no harness", "-", "—"):
            self.assertIs(outcome.parse("Result: done\nVerified: " + value)["verified"], False, value)
        for value in ("ran pytest", "`npm test` green", "✓ unittest", "nonexistent-file check passed", "notebook re-run"):
            self.assertIs(outcome.parse("Result: done\nVerified: " + value)["verified"], True, value)

    def test_an_echoed_contract_template_is_not_an_outcome(self):
        template = ("End your reply with two lines: `Result: done|partial|blocked|escalate` and\n"
                    "`Verified: <the command or evidence you ran, or none>`.")
        self.assertEqual(outcome.parse(template), {"outcome": "unknown", "verified": None})
        self.assertEqual(outcome.parse("Result: done\nVerified: pytest\n\n" + template), {"outcome": "done", "verified": True})
        for line in ("Result: done|partial", "Result: done/blocked", "Result: doneish"):
            self.assertEqual(outcome.parse(line)["outcome"], "unknown", line)
        for line in ("Result: done.", "Result: `done` (green)", "Result: **done**", "Result: done, with notes"):
            self.assertEqual(outcome.parse(line)["outcome"], "done", line)

    def test_only_the_tail_is_read(self):
        text = "Result: done\n" + ("x\n" * 5000) + "Result: blocked"
        self.assertEqual(outcome.parse(text)["outcome"], "blocked")
        buried = "Result: done\n" + ("filler line\n" * 100)
        self.assertEqual(outcome.parse(buried)["outcome"], "unknown")


class Escalation(unittest.TestCase):
    def test_each_tier_and_its_dressing(self):
        for tier in outcome.TIERS:
            self.assertEqual(outcome.escalation_tier("Escalation from %s: the check failed" % tier), tier)
        self.assertEqual(outcome.escalation_tier("**Escalation from Cheap:** tests red\n\nfix it"), "cheap")
        self.assertEqual(outcome.escalation_tier("\n\n  escalation from standard : x"), "standard")

    def test_absent_or_buried_markers_do_not_count(self):
        self.assertIsNone(outcome.escalation_tier("Investigate the regression"))
        self.assertIsNone(outcome.escalation_tier("Escalation from parent: nope"))
        self.assertIsNone(outcome.escalation_tier(("line\n" * 20) + "Escalation from cheap: late"))
        self.assertIsNone(outcome.escalation_tier(None))


if __name__ == "__main__":
    unittest.main()
