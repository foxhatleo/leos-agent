"""Behavioral tests for learning from the user's edits to staged reviews."""

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ghreview import GitHubCase, _no_real_gh, ghreview, text_file  # noqa: E402

import review_learnings  # noqa: E402

_guard = mock.patch.object(subprocess, "run", _no_real_gh)


def setUpModule():
    _guard.start()


def tearDownModule():
    _guard.stop()


def identity(comment):
    return ghreview.identity_hash(comment)


def row(path, line, severity=None, category=None, pattern="p"):
    return {"path": path, "line": line, "side": "RIGHT", "severity": severity,
            "category": category, "pattern": pattern}


def submitted(path, line, body, reply_to=None):
    return {"path": path, "line": line, "original_line": line, "body": body, "in_reply_to_id": reply_to}


class TestClassify(unittest.TestCase):
    def test_unchanged_comments_teach_nothing(self):
        kept = submitted("a.py", 3, "loop never ends")
        events = review_learnings.classify({identity(kept): row("a.py", 3)}, [kept], identity)
        self.assertEqual(events, [])

    def test_a_missing_comment_was_deleted(self):
        gone = submitted("a.py", 3, "loop never ends")
        events = review_learnings.classify({identity(gone): row("a.py", 3, "minor")}, [], identity)
        self.assertEqual([e["action"] for e in events], ["deleted"])

    def test_new_text_at_the_same_anchor_was_rewritten(self):
        staged = submitted("a.py", 3, "loop never ends")
        events = review_learnings.classify({identity(staged): row("a.py", 3, "major")},
                                           [submitted("a.py", 3, "the loop can spin forever on EOF")], identity)
        self.assertEqual([e["action"] for e in events], ["rewritten"])

    def test_a_lower_severity_label_is_a_downgrade(self):
        staged = submitted("a.py", 3, "loop never ends")
        for label in ("nit: loop", "Minor - loop", "**optional**: loop"):
            with self.subTest(label=label):
                events = review_learnings.classify({identity(staged): row("a.py", 3, "major")},
                                                   [submitted("a.py", 3, label)], identity)
                self.assertEqual([e["action"] for e in events], ["downgraded"])

    def test_an_edit_pairs_by_the_pending_position_when_no_line_is_reported(self):
        a, b = submitted("a.py", 3, "first"), submitted("a.py", 9, "second")
        rows = {identity(a): dict(row("a.py", 3, "major"), position=2),
                identity(b): dict(row("a.py", 9, "major"), position=8)}
        edited = {"path": "a.py", "line": None, "original_line": None, "original_position": 8,
                  "body": "nit: second", "in_reply_to_id": None}
        events = review_learnings.classify(rows, [edited], identity)
        self.assertEqual(sorted(e["action"] for e in events), ["deleted", "downgraded"])

    def test_a_reply_or_another_line_is_not_mistaken_for_the_edit(self):
        a = submitted("a.py", 3, "first")
        b = submitted("a.py", 9, "second")
        rows = {identity(a): row("a.py", 3), identity(b): row("a.py", 9)}
        events = review_learnings.classify(
            rows, [submitted("a.py", 3, "first, reworded"), submitted("a.py", 3, "a reply", reply_to=5)], identity)
        self.assertEqual(sorted(e["action"] for e in events), ["deleted", "rewritten"])


class TestStoredShape(unittest.TestCase):
    def test_pattern_keeps_the_shape_of_a_finding_and_drops_code_numbers_links_and_controls(self):
        body = ("<!-- leos-agent:review-pr -->\nnit: `parse_token(secret_value)` retries 3 times\x1b[31m "
                "see https://example.com/x?token=abc‮ and more words beyond the twelve word limit here")
        result = review_learnings.pattern(body)
        for leaked in ("secret_value", "parse_token", "3", "example.com", "\x1b", "‮", "leos-agent"):
            self.assertNotIn(leaked, result)
        self.assertTrue(result.startswith("nit <code> retries # times"))
        self.assertLessEqual(len(result.split()), review_learnings.PATTERN_WORDS)

    def test_path_glob_keeps_directory_and_extension(self):
        self.assertEqual(review_learnings.path_glob("src/auth/token.py"), "src/auth/*.py")
        self.assertEqual(review_learnings.path_glob("Makefile"), "*")


class LearningCase(GitHubCase):
    FILES = [text_file("a.py", "@@ -1,1 +1,4 @@\n ctx\n+one\n+two\n+three")]
    COMMENTS = [
        {"path": "a.py", "line": 2, "side": "RIGHT", "body": "this branch swallows `KeyError` from 3 callers",
         "severity": "minor", "category": "error-handling"},
        {"path": "a.py", "line": 3, "side": "RIGHT", "body": "the retry loop never backs off",
         "severity": "major", "category": "performance"},
        {"path": "a.py", "line": 4, "side": "RIGHT", "body": "off-by-one: the last row is skipped",
         "severity": "major", "category": "correctness"},
    ]

    def staged_review(self):
        fake = self.github(self.H1, self.FILES)
        code, report, err = self.stage("Owner/Repo", {"verdict": "neutral", "reviewed": ["a.py"],
                                                      "comments": self.COMMENTS}, self.H1)
        self.assertEqual(code, 0, err)
        return fake, report["review_id"]

    def submit(self, fake, rid, delete_line=None, rewrite=None):
        """What GitHub shows once the user submits the draft after editing it."""
        comments = []
        for line, c in zip((2, 3, 4), fake.comments[rid]):
            if line == delete_line:
                continue
            c = dict(c, line=line, original_line=line, side="RIGHT")
            if rewrite and line == rewrite[0]:
                c["body"] = rewrite[1]
            comments.append(c)
        fake.comments[rid] = comments
        fake.reviews[rid]["state"] = "COMMENTED"

    def learnings(self):
        code, out, err = self.main("learnings", "-R", "Owner/Repo", "list")
        self.assertEqual(code, 0, err)
        return json.loads(out)["entries"]


class TestLearningFromSubmittedReviews(LearningCase):
    def test_staged_severity_and_category_stay_local(self):
        fake, _ = self.staged_review()
        for comment in fake.posted[0]["comments"]:
            self.assertNotIn("severity", comment)
            self.assertNotIn("category", comment)

    def test_pending_learns_deletions_and_downgrades_once(self):
        fake, rid = self.staged_review()
        self.submit(fake, rid, delete_line=2, rewrite=(3, "nit: SUBMITTED-TEXT-MARKER consider backoff"))
        code, out, err = self.main("pending", "-R", "Owner/Repo", "-n", "1")
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn("learned 2 edit(s)", err)
        entries = self.learnings()
        by_action = {e["action"]: e for e in entries}
        self.assertEqual(set(by_action), {"deleted", "downgraded"})
        self.assertEqual(by_action["deleted"]["category"], "error-handling")
        self.assertEqual(by_action["deleted"]["path_glob"], "*.py")
        self.assertEqual(by_action["downgraded"]["severity"], "major")
        path = Path(self.tmp.name) / "review-learnings" / "Owner__Repo.json"
        self.assertNotIn("SUBMITTED-TEXT-MARKER", path.read_text())
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
        # A second pass reads nothing more and counts nothing twice.
        reads = len(fake.calls)
        self.main("pending", "-R", "Owner/Repo", "-n", "1")
        self.assertFalse(any("/reviews/%d/comments" % rid in " ".join(c["args"]) for c in fake.calls[reads:]))
        self.assertEqual([e["count"] for e in self.learnings()], [1, 1])

    def test_a_review_not_staged_here_is_never_read(self):
        fake = self.github(self.H1, self.FILES)
        fake.reviews[500] = {"id": 500, "node_id": "R500", "state": "COMMENTED", "user": {"login": "me"},
                             "body": "", "commit_id": self.H1}
        fake.comments[500] = [submitted("a.py", 2, "hand written")]
        code, _, err = self.main("learnings", "-R", "Owner/Repo", "-n", "1", "harvest")
        self.assertEqual(code, 0, err)
        self.assertFalse(any("/reviews/500/comments" in " ".join(c["args"]) for c in fake.calls))
        self.assertEqual(self.learnings(), [])

    def test_a_corrupt_learnings_file_never_breaks_pending(self):
        fake, rid = self.staged_review()
        self.submit(fake, rid, delete_line=2)
        path = Path(self.tmp.name) / "review-learnings" / "Owner__Repo.json"
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text("[not a learnings file")
        code, _, err = self.main("pending", "-R", "Owner/Repo", "-n", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("review learnings not updated", err)
        self.assertEqual(path.read_text(), "[not a learnings file")

    def test_forget_removes_entries_and_the_same_review_is_not_learned_again(self):
        fake, rid = self.staged_review()
        self.submit(fake, rid, delete_line=2, rewrite=(3, "nit: backoff"))
        self.main("learnings", "-R", "Owner/Repo", "-n", "1", "harvest")
        first = self.learnings()[0]["id"]
        code, out, err = self.main("learnings", "-R", "Owner/Repo", "forget", "--id", first)
        self.assertEqual((code, json.loads(out)["forgotten"]), (0, 1), err)
        self.assertNotIn(first, [e["id"] for e in self.learnings()])
        code, _, err = self.main("learnings", "-R", "Owner/Repo", "forget", "--all")
        self.assertEqual(code, 0, err)
        self.main("learnings", "-R", "Owner/Repo", "-n", "1", "harvest")
        self.assertEqual(self.learnings(), [])

    def test_forget_needs_an_id_or_all(self):
        self.github(self.H1, self.FILES)
        code, _, err = self.main("learnings", "-R", "Owner/Repo", "forget")
        self.assertEqual(code, 2)
        self.assertIn("--id", err)


class TestSuppressionInStage(LearningCase):
    def learned_id(self):
        fake, rid = self.staged_review()
        self.submit(fake, rid, delete_line=2)
        self.main("learnings", "-R", "Owner/Repo", "-n", "1", "harvest")
        return fake, self.learnings()[0]["id"]

    def stage_with(self, suppressed):
        return self.stage("Owner/Repo", {"verdict": "neutral", "reviewed": ["a.py"], "notes": ["question"],
                                         "suppressed": suppressed}, self.H1, "--dry-run")

    def test_a_non_blocking_suppression_is_reported(self):
        _, learning = self.learned_id()
        code, report, err = self.stage_with([{"path": "a.py", "line": 2, "severity": "minor",
                                              "action": "omitted", "learning": learning,
                                              "reason": "the user deletes swallowed-error nits here\x1b"}])
        self.assertEqual(code, 0, err)
        self.assertEqual(report["suppressed"][0]["learning"], learning)
        self.assertNotIn("\x1b", report["suppressed"][0]["reason"])

    def test_a_blocking_finding_is_never_suppressed(self):
        fake, learning = self.learned_id()
        posted = len(fake.posted)
        code, _, err = self.stage_with([{"path": "a.py", "severity": "blocking", "action": "omitted",
                                         "learning": learning, "reason": "learned"}])
        self.assertEqual(code, 2)
        self.assertIn("blocking", err)
        self.assertEqual(len(fake.posted), posted)

    def test_a_major_finding_is_downgraded_never_omitted(self):
        fake, learning = self.learned_id()
        posted = len(fake.posted)
        code, _, err = self.stage_with([{"path": "a.py", "severity": "major", "action": "omitted",
                                         "learning": learning, "reason": "learned"}])
        self.assertEqual(code, 2)
        self.assertIn("major", err)
        self.assertEqual(len(fake.posted), posted)
        for severity, action in (("major", "downgraded"), ("minor", "omitted"), ("nit", "omitted")):
            with self.subTest(severity=severity, action=action):
                code, report, err = self.stage_with([{"path": "a.py", "severity": severity, "action": action,
                                                      "learning": learning, "reason": "learned"}])
                self.assertEqual(code, 0, err)
                self.assertEqual(report["suppressed"][0]["action"], action)

    def test_a_suppression_must_name_a_recorded_learning(self):
        self.learned_id()
        code, _, err = self.stage_with([{"path": "a.py", "severity": "minor", "action": "omitted",
                                         "learning": "0123456789ab", "reason": "made up"}])
        self.assertEqual(code, 2)
        self.assertIn("no recorded learning", err)

    def test_an_unknown_severity_is_refused_before_staging(self):
        fake = self.github(self.H1, self.FILES)
        code, _, err = self.stage("Owner/Repo", {"verdict": "neutral", "reviewed": ["a.py"], "comments": [
            dict(self.COMMENTS[0], severity="critical")]}, self.H1)
        self.assertEqual(code, 2)
        self.assertIn("severity", err)
        self.assertEqual(fake.posted, [])


class TestValidationRecord(LearningCase):
    BLOCKING = {"path": "a.py", "line": 4, "side": "RIGHT", "body": "off-by-one: the last row is skipped",
                "severity": "blocking", "category": "correctness"}

    def stage_blocking(self, validation, *extra):
        return self.stage("Owner/Repo", {"verdict": "seriously-problematic", "reviewed": ["a.py"],
                                         "comments": [self.BLOCKING], "validation": validation}, self.H1, *extra)

    def test_validation_is_recorded_with_the_verdict(self):
        self.github(self.H1, self.FILES)
        code, report, err = self.stage_blocking([
            {"path": "a.py", "line": 4, "result": "CONFIRMED", "independent": True,
             "evidence": "a.py:4 loops to len-1\x1b[0m"}])
        self.assertEqual(code, 0, err)
        self.assertEqual(report["validation"][0]["result"], "CONFIRMED")
        self.assertNotIn("\x1b", report["validation"][0]["evidence"])
        record = ghreview.load_verdict("Owner/Repo", 1)
        self.assertEqual(record["validation"], report["validation"])

    def test_validation_is_optional_and_self_checks_are_kept_distinct(self):
        self.github(self.H1, self.FILES)
        code, report, err = self.stage_blocking(None)
        self.assertEqual(code, 0, err)
        self.assertNotIn("validation", ghreview.load_verdict("Owner/Repo", 1))
        code, report, err = self.stage_blocking([
            {"path": "a.py", "line": 4, "result": "UNCERTAIN", "independent": False}], "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIs(report["validation"][0]["independent"], False)

    def test_malformed_validation_is_refused_before_staging(self):
        fake = self.github(self.H1, self.FILES)
        for bad in ([{"path": "a.py", "result": "PROBABLY", "independent": True}],
                    [{"path": "a.py", "result": "CONFIRMED"}],
                    [{"result": "REFUTED", "independent": True}],
                    {"path": "a.py"}):
            with self.subTest(bad=bad):
                code, _, err = self.stage_blocking(bad)
                self.assertEqual(code, 2)
                self.assertIn("validation", err)
        self.assertEqual(fake.posted, [])


if __name__ == "__main__":
    unittest.main()
