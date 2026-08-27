"""Behavioral tests for review staging and diff-line validation."""

import importlib.util
import json
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
