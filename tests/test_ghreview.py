"""Behavioral tests for review staging and diff-line validation."""

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
import os
import tempfile
import types
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ghreview_test", ROOT / "scripts" / "ghreview.py")
ghreview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ghreview)


class TestPatchParsing(unittest.TestCase):
    def test_additions_and_deletions_land_on_the_correct_sides(self):
        patch = "@@ -1,3 +1,3 @@\n context\n-old\n+new\n tail\n"
        parsed = ghreview.parse_patch(patch)
        self.assertEqual(parsed["left"], {2})
        self.assertEqual(parsed["right"], {1, 2, 3})

    def test_snap_drops_a_distant_line(self):
        diffmap = {"right": {20}, "left": set(), "hunks": [{"r": (10, 20), "l": (10, 20)}]}
        self.assertIsNone(ghreview.snap_line(diffmap, "RIGHT", 9))


class TestCommentValidation(unittest.TestCase):
    def setUp(self):
        self.maps = {
            "a.py": {
                "right": set(range(10, 21)),
                "left": {12},
                "hunks": [{"r": (10, 20), "l": (10, 20)}],
            }
        }

    def test_valid_comment_is_marked_once(self):
        comments = [{"path": "a.py", "line": 15, "side": "RIGHT", "body": "fix this"}]
        staged, snapped, dropped = ghreview.validate_comments(comments, self.maps)
        self.assertEqual(snapped, [])
        self.assertEqual(dropped, [])
        self.assertEqual(staged[0]["body"].count(ghreview.MARKER), 1)

    def test_invalid_side_is_dropped_before_github(self):
        staged, _, dropped = ghreview.validate_comments(
            [{"path": "a.py", "line": 15, "side": "SIDEWAYS", "body": "x"}], self.maps
        )
        self.assertEqual(staged, [])
        self.assertIn("invalid side", dropped[0]["reason"])

    def test_bool_line_is_not_accepted_as_an_integer(self):
        staged, _, dropped = ghreview.validate_comments(
            [{"path": "a.py", "line": True, "side": "RIGHT", "body": "x"}], self.maps
        )
        self.assertEqual(staged, [])
        self.assertEqual(dropped[0]["reason"], "missing path/line/body")

    def test_null_body_and_non_object_do_not_crash(self):
        staged, _, dropped = ghreview.validate_comments(
            [{"path": "a.py", "line": 15, "body": None}, "not-an-object"], self.maps
        )
        self.assertEqual(staged, [])
        self.assertEqual(len(dropped), 2)


class TestPendingReviewSafety(unittest.TestCase):
    def test_post_payload_omits_event(self):
        calls = []
        original = ghreview.gh

        def fake_gh(args, payload=None):
            calls.append((args, json.loads(payload)))
            return '{"id": 7, "state": "PENDING"}'

        ghreview.gh = fake_gh
        try:
            result = ghreview.post_review("o/r", 4, "abc", [{"path": "a.py"}])
        finally:
            ghreview.gh = original
        self.assertEqual(result["state"], "PENDING")
        self.assertNotIn("event", calls[0][1])

    def test_unmarked_pending_comment_refuses_deletion(self):
        original_pending = ghreview.pending_review
        original_comments = ghreview.review_comments
        original_gh = ghreview.gh
        ghreview.pending_review = lambda repo, pr: {"id": 9, "node_id": "R9"}
        ghreview.review_comments = lambda repo, pr, review_id: [{"body": "my draft"}]
        ghreview.gh = lambda *args, **kwargs: self.fail("delete must not run")
        try:
            result, refusal = ghreview.clear_pending_guarded("o/r", 4, False)
        finally:
            ghreview.pending_review = original_pending
            ghreview.review_comments = original_comments
            ghreview.gh = original_gh
        self.assertIsNone(result)
        self.assertTrue(refusal["refused"])

    def test_stage_refuses_more_comments_than_the_cap(self):
        # Blast-radius backstop: even a reviewer talked past its own 15-comment
        # cap cannot blanket a pull request. Refusal happens before any gh call.
        import tempfile

        comment = {"path": "a.py", "line": 1, "side": "RIGHT", "body": "x"}
        payload = {"comments": [comment] * (ghreview.MAX_STAGE_COMMENTS + 1)}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(payload, fh)
            path = fh.name
        original_gh = ghreview.gh
        ghreview.gh = lambda *args, **kwargs: self.fail("no gh call may happen past the cap")
        try:
            import types

            args = types.SimpleNamespace(repo="o/r", pr=4, input=path, commit="abc",
                                         replace_pending=False, force=False, dry_run=False)
            with self.assertRaises(SystemExit) as ctx:
                ghreview.cmd_stage(args)
        finally:
            ghreview.gh = original_gh
        self.assertEqual(ctx.exception.code, 2)


class TestPinnedReviewSafety(unittest.TestCase):
    SHA = "a" * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def test_empty_unowned_pending_review_is_preserved(self):
        for body in ("", "My summary"):
            with mock.patch.object(ghreview, "pending_review", return_value={"id": 1, "body": body}), \
                 mock.patch.object(ghreview, "review_comments", return_value=[]), \
                 mock.patch.object(ghreview, "gh") as api:
                _, refusal = ghreview.clear_pending_guarded("o/r", 1, False)
                self.assertTrue(refusal["refused"])
                api.assert_not_called()

    def test_marker_remaining_after_user_edit_does_not_allow_deletion(self):
        comments = [{"body": ghreview._mark("original"), "path": "a.py", "line": 1, "side": "RIGHT"}]
        review = {"id": 1, "body": ""}
        ghreview.remember_review("o/r", 1, review, comments)
        comments[0]["body"] = ghreview._mark("user edited")
        with mock.patch.object(ghreview, "pending_review", return_value=review), \
             mock.patch.object(ghreview, "review_comments", return_value=comments), \
             mock.patch.object(ghreview, "gh") as api:
            _, refusal = ghreview.clear_pending_guarded("o/r", 1, False)
            self.assertTrue(refusal["refused"])
            api.assert_not_called()

    def test_moving_head_is_refused_before_posting(self):
        path = self.root / "comments.json"
        path.write_text(json.dumps([{"path": "a.py", "line": 1, "body": "finding"}]))
        args = types.SimpleNamespace(repo="o/r", pr=1, commit=self.SHA, input=path,
                                     replace_pending=True, force=False, dry_run=False)
        with mock.patch.object(ghreview, "current_head", side_effect=[self.SHA, "b" * 40]), \
             mock.patch.object(ghreview, "fetch_files", return_value=[]), \
             mock.patch.object(ghreview, "post_review") as post, \
             mock.patch.object(ghreview, "clear_pending_guarded") as delete:
            with self.assertRaisesRegex(ValueError, "head changed"):
                ghreview.cmd_stage(args)
            post.assert_not_called()
            delete.assert_not_called()

    def test_resolution_refuses_foreign_thread_or_another_authors_thread(self):
        evidence = self.root / "evidence.json"
        evidence.write_text(json.dumps({"thread_id": "T", "head_sha": self.SHA, "explanation": "fixed in current code"}))
        args = types.SimpleNamespace(repo="o/r", pr=1, commit=self.SHA, thread_id="T",
                                     evidence_file=evidence, dry_run=False)
        for threads in ([], [{"id": "T", "comments": {"nodes": [{"author": {"login": "other"}}]}}]):
            with mock.patch.object(ghreview, "current_head", return_value=self.SHA), \
                 mock.patch.object(ghreview, "fetch_threads", return_value=threads), \
                 mock.patch.object(ghreview, "current_login", return_value="me"), \
                 mock.patch.object(ghreview, "graphql") as mutate:
                with self.assertRaises(ValueError):
                    ghreview.cmd_resolve_thread(args)
                mutate.assert_not_called()

    def test_reply_validates_membership_before_creating_draft(self):
        args = types.SimpleNamespace(repo="o/r", pr=1, thread_id="other-pr-thread")
        with mock.patch.object(ghreview, "fetch_threads", return_value=[]), \
             mock.patch.object(ghreview, "post_review") as post:
            with self.assertRaises(ValueError):
                ghreview.cmd_reply(args)
            post.assert_not_called()

    def test_exact_anchor_required_even_one_line_away(self):
        self.assertIsNone(ghreview.snap_line({"right": {10}, "hunks": [{"r": (10, 10)}]}, "RIGHT", 9))


class TestCarriedFindings(unittest.TestCase):
    """A finding that cannot be anchored must still reach the author."""

    def test_a_finding_that_kept_a_path_and_a_body_is_representable(self):
        self.assertTrue(ghreview.representable({"path": "a.py", "body": "text", "reason": "r"}))
        self.assertFalse(ghreview.representable({"path": "a.py", "body": "   "}))
        self.assertFalse(ghreview.representable({"body": "text"}))
        self.assertFalse(ghreview.representable("not an object"))

    def test_the_rendered_body_names_path_line_side_and_reason(self):
        body, carried, overflow = ghreview.render_carried([
            {"path": "a.py", "line": 42, "side": "LEFT", "body": "the finding",
             "reason": "line 42 (LEFT) not addressable in any hunk"},
        ])
        self.assertEqual((len(carried), len(overflow)), (1, 0))
        for needle in ("a.py:42", "LEFT", "not addressable", "the finding", ghreview.MARKER):
            self.assertIn(needle, body)

    def test_a_finding_that_does_not_fit_is_omitted_rather_than_quietly_cut(self):
        """Coverage honesty: a finding nobody can read has not been preserved
        just because we tried, so it must be reported as omitted, not carried."""
        findings = [{"path": f"f{i}.py", "line": i, "side": "RIGHT",
                     "body": "x" * ghreview.MAX_FINDING_CHARS, "reason": "r"} for i in range(60)]
        body, carried, overflow = ghreview.render_carried(findings)
        self.assertTrue(overflow)
        self.assertEqual(len(carried) + len(overflow), len(findings))
        self.assertLessEqual(len(body), ghreview.MAX_BODY_CHARS + 200)
        self.assertIn("did not fit", body)
        self.assertTrue(all("did not fit" in o["reason"] for o in overflow))

    def test_one_huge_finding_cannot_crowd_out_the_others(self):
        body, carried, _ = ghreview.render_carried([
            {"path": "big.py", "line": 1, "side": "RIGHT", "body": "y" * 50000, "reason": "r"},
            {"path": "small.py", "line": 2, "side": "RIGHT", "body": "short", "reason": "r"},
        ])
        self.assertEqual(len(carried), 2)
        self.assertIn("small.py", body)


class TestStageCoverage(unittest.TestCase):
    """cmd_stage's report is what watch_review trusts to close out a head."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.files = [{"filename": "a.py", "additions": 1, "deletions": 0,
                       "patch": "@@ -1,1 +1,1 @@\n+anchored\n"}]

    def stage(self, comments, **overrides):
        path = Path(self.tmp.name) / "in.json"
        path.write_text(json.dumps({"comments": comments}))
        args = types.SimpleNamespace(repo="o/r", pr=1, commit="a" * 40, input=str(path),
                                     replace_pending=False, force=False, dry_run=False)
        for key, value in overrides.items():
            setattr(args, key, value)
        posted = {}

        def fake_post(repo, pr, commit, staged, body=""):
            posted.update(staged=staged, body=body)
            return {"id": 7, "node_id": "n", "state": "PENDING", "body": body}

        buffer = io.StringIO()
        with mock.patch.object(ghreview, "pinned_files", return_value=self.files), \
             mock.patch.object(ghreview, "require_head"), \
             mock.patch.object(ghreview, "post_review", side_effect=fake_post), \
             mock.patch.object(ghreview, "remember_review") as remember, \
             contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            ghreview.cmd_stage(args)
        return json.loads(buffer.getvalue()), posted, remember

    def test_a_partly_anchored_review_carries_the_rest_and_is_complete(self):
        """The reproduced deadlock: one unanchorable finding used to make the
        whole head unrecordable. It is now carried, and the review is complete."""
        report, posted, _ = self.stage([
            {"path": "a.py", "line": 1, "body": "anchored finding"},
            {"path": "a.py", "line": 999, "body": "no line for this one"},
        ])
        self.assertEqual((report["staged"], report["carried"], report["omitted"]), (1, 1, []))
        self.assertTrue(report["complete"])
        self.assertTrue(report["review_created"])
        self.assertIn("no line for this one", posted["body"])

    def test_findings_with_no_anchorable_line_still_create_a_review(self):
        report, posted, _ = self.stage([{"path": "a.py", "line": 999, "body": "only finding"}])
        self.assertTrue(report["complete"])
        self.assertTrue(report["review_created"])
        self.assertEqual(posted["staged"], [])
        self.assertIn("only finding", posted["body"])

    def test_malformed_input_is_omitted_and_the_report_is_not_complete(self):
        report, _, _ = self.stage([
            {"path": "a.py", "line": 1, "body": "anchored"},
            {"path": "a.py", "line": 5, "body": "   "},
        ])
        self.assertEqual(len(report["omitted"]), 1)
        self.assertFalse(report["complete"])

    def test_nothing_renderable_creates_no_review(self):
        report, posted, _ = self.stage([{"line": 5, "body": ""}])
        self.assertFalse(report["review_created"])
        self.assertFalse(report["complete"])
        self.assertEqual(posted, {})

    def test_the_receipt_records_the_body_github_returned(self):
        """The trap. remember_review used to hash body="" while a non-empty body
        was posted, so pending_snapshot stopped recognising our own draft and
        every later --replace-pending refused -- on the NEXT review, not this one."""
        _, posted, remember = self.stage([{"path": "a.py", "line": 999, "body": "carried"}])
        self.assertEqual(remember.call_args[0][4], posted["body"])
        self.assertTrue(posted["body"])

    def test_an_owned_draft_with_a_carried_body_is_still_recognised_later(self):
        comments = [{"path": "a.py", "line": 1, "side": "RIGHT", "body": "anchored"}]
        body, _, _ = ghreview.render_carried([
            {"path": "a.py", "line": 9, "side": "RIGHT", "body": "carried", "reason": "r"}])
        fingerprint = ghreview.review_fingerprint(body, comments)
        review = {"id": 7, "body": body}
        with mock.patch.object(ghreview, "pending_review", return_value=review), \
             mock.patch.object(ghreview, "review_comments", return_value=comments), \
             mock.patch.object(ghreview, "receipt_path") as receipt:
            receipt.return_value = Path(self.tmp.name) / "receipt.json"
            receipt.return_value.write_text(json.dumps({"fingerprint": fingerprint, "review_id": 7}))
            snapshot, refusal = ghreview.pending_snapshot("o/r", 1)
        self.assertIsNone(refusal)
        self.assertFalse(snapshot["forced"])


if __name__ == "__main__":
    unittest.main()
