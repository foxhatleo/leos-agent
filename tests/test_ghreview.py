"""Behavioral tests for review staging and diff-line validation."""

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
