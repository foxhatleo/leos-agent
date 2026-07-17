#!/usr/bin/env python3
"""Tests for skills/review-pr/scripts/ghreview.py."""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GHREVIEW_PY = os.path.join(REPO, "skills", "review-pr", "scripts", "ghreview.py")

spec = importlib.util.spec_from_file_location("ghreview", GHREVIEW_PY)
ghreview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ghreview)


def make_diffmap(hunks):
    """hunks: list of (r_start, r_end, l_start, l_end) -> a diffmap where every
    line in [r_start, r_end] is addressable on RIGHT and every line in
    [l_start, l_end] is addressable on LEFT."""
    right, left, out_hunks = set(), set(), []
    for r_start, r_end, l_start, l_end in hunks:
        right.update(range(r_start, r_end + 1))
        left.update(range(l_start, l_end + 1))
        out_hunks.append({"r": (r_start, r_end), "l": (l_start, l_end)})
    return {"right": right, "left": left, "hunks": out_hunks}


def make_sparse_diffmap(hunk, right_lines):
    """A single hunk (r_start, r_end, l_start, l_end) whose addressable RIGHT
    lines are exactly right_lines (a sparse subset of the hunk's range) —
    lets a test pin down exactly which line snap_line should land on."""
    r_start, r_end, l_start, l_end = hunk
    return {
        "right": set(right_lines),
        "left": set(),
        "hunks": [{"r": (r_start, r_end), "l": (l_start, l_end)}],
    }


class TestGraphqlArgs(unittest.TestCase):
    """graphql() dispatches variables to -F/-f based on type; bool must be
    checked before int since bool is an int subclass."""

    def setUp(self):
        self.calls = []

        def fake_gh(args, payload=None):
            self.calls.append(args)
            return "{}"

        self._orig_gh = ghreview.gh
        ghreview.gh = fake_gh

    def tearDown(self):
        ghreview.gh = self._orig_gh

    def test_int_uses_dash_F(self):
        ghreview.graphql("query", {"number": 42})
        args = self.calls[0]
        self.assertIn("-F", args)
        self.assertIn("number=42", args)

    def test_str_uses_dash_f(self):
        ghreview.graphql("query", {"owner": "leo"})
        args = self.calls[0]
        self.assertIn("-f", args)
        self.assertIn("owner=leo", args)

    def test_bool_true_uses_dash_F_lowercase(self):
        ghreview.graphql("query", {"flag": True})
        args = self.calls[0]
        self.assertIn("-F", args)
        self.assertIn("flag=true", args)
        self.assertNotIn("flag=True", args)

    def test_bool_false_uses_dash_F_lowercase(self):
        ghreview.graphql("query", {"flag": False})
        args = self.calls[0]
        self.assertIn("-F", args)
        self.assertIn("flag=false", args)
        self.assertNotIn("flag=False", args)


class TestSnapLine(unittest.TestCase):
    def test_exact_line_returned_as_is(self):
        diffmap = make_diffmap([(10, 20, 10, 20)])
        self.assertEqual(ghreview.snap_line(diffmap, "RIGHT", 15), 15)

    def test_snaps_within_tolerance_outside_hunk(self):
        diffmap = make_diffmap([(10, 20, 10, 20)])
        # line 22 is outside [10,20] but within SNAP_TOLERANCE (3) of it,
        # and the nearest addressable line (20) is within SNAP_MAX_DISTANCE.
        self.assertEqual(ghreview.snap_line(diffmap, "RIGHT", 22), 20)

    def test_picks_nearest_across_multiple_hunks(self):
        diffmap = make_diffmap([(1, 5, 1, 5), (100, 105, 100, 105)])
        self.assertEqual(ghreview.snap_line(diffmap, "RIGHT", 4), 4)
        self.assertEqual(ghreview.snap_line(diffmap, "RIGHT", 98), 100)

    def test_none_when_no_hunk_matches(self):
        diffmap = make_diffmap([(10, 20, 10, 20)])
        self.assertIsNone(ghreview.snap_line(diffmap, "RIGHT", 1000))

    def test_distance_guard_drops_beyond_max(self):
        # Hunk spans 10..200 but the only addressable RIGHT line is 200 (the
        # rest is unmodified context). A query for line 9 is within
        # SNAP_TOLERANCE (3) of the hunk start (10), so the hunk matches, but
        # the nearest candidate (200) is far beyond SNAP_MAX_DISTANCE (10) —
        # must drop rather than snap onto unrelated code.
        diffmap = make_sparse_diffmap((10, 200, 10, 200), [200])
        self.assertIsNone(ghreview.snap_line(diffmap, "RIGHT", 9))

    def test_keeps_within_max_distance(self):
        # Same setup, but the query line is close enough to the sole
        # candidate (distance == SNAP_MAX_DISTANCE exactly) to be kept.
        diffmap = make_sparse_diffmap((10, 200, 10, 200), [200])
        near_line = 200 - ghreview.SNAP_MAX_DISTANCE
        self.assertEqual(ghreview.snap_line(diffmap, "RIGHT", near_line), 200)


class TestValidateComments(unittest.TestCase):
    def setUp(self):
        self.maps = {
            "a.py": make_diffmap([(10, 20, 10, 20)]),
        }

    def test_valid_comment_gets_marker_appended_once(self):
        comments = [{"path": "a.py", "line": 15, "side": "RIGHT", "body": "fix this"}]
        staged, snapped, dropped = ghreview.validate_comments(comments, self.maps)
        self.assertEqual(len(staged), 1)
        self.assertEqual(dropped, [])
        self.assertIn(ghreview.MARKER, staged[0]["body"])
        self.assertEqual(staged[0]["body"].count(ghreview.MARKER), 1)

    def test_off_diff_line_gets_snapped_and_reported(self):
        comments = [{"path": "a.py", "line": 22, "side": "RIGHT", "body": "hi"}]
        staged, snapped, dropped = ghreview.validate_comments(comments, self.maps)
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["line"], 20)
        self.assertEqual(len(snapped), 1)
        self.assertEqual(snapped[0], {"path": "a.py", "from": 22, "to": 20})

    def test_unaddressable_line_dropped(self):
        comments = [{"path": "a.py", "line": 9999, "side": "RIGHT", "body": "hi"}]
        staged, snapped, dropped = ghreview.validate_comments(comments, self.maps)
        self.assertEqual(staged, [])
        self.assertEqual(len(dropped), 1)

    def test_missing_body_dropped(self):
        comments = [{"path": "a.py", "line": 15, "side": "RIGHT", "body": ""}]
        staged, snapped, dropped = ghreview.validate_comments(comments, self.maps)
        self.assertEqual(staged, [])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["reason"], "missing path/line/body")


class TestMarker(unittest.TestCase):
    def test_appends_once(self):
        marked = ghreview._mark("hello")
        self.assertTrue(marked.startswith("hello"))
        self.assertIn(ghreview.MARKER, marked)
        self.assertEqual(marked.count(ghreview.MARKER), 1)

    def test_idempotent(self):
        once = ghreview._mark("hello")
        twice = ghreview._mark(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(ghreview.MARKER), 1)


class TestClearPendingGuard(unittest.TestCase):
    def setUp(self):
        self._orig_gh = ghreview.gh
        self._orig_pending_review = ghreview.pending_review
        self._orig_review_comments = ghreview.review_comments
        self.delete_calls = []

        def fake_gh(args, payload=None):
            self.delete_calls.append(args)
            return "{}"

        ghreview.gh = fake_gh

    def tearDown(self):
        ghreview.gh = self._orig_gh
        ghreview.pending_review = self._orig_pending_review
        ghreview.review_comments = self._orig_review_comments

    def _stub(self, review, comments):
        ghreview.pending_review = lambda repo, pr: review
        ghreview.review_comments = lambda repo, pr, review_id: comments

    def test_no_pending_review_is_a_noop(self):
        self._stub(None, [])
        result, refusal = ghreview.clear_pending_guarded("o/r", 1, False)
        self.assertIsNone(refusal)
        self.assertEqual(result, {"deleted": None})
        self.assertEqual(self.delete_calls, [])

    def test_refuses_unmarked_without_force(self):
        review = {"id": 1, "node_id": "n1"}
        comments = [{"body": "hand drafted comment, no marker"}]
        self._stub(review, comments)
        result, refusal = ghreview.clear_pending_guarded("o/r", 1, False)
        self.assertIsNone(result)
        self.assertTrue(refusal["refused"])
        self.assertEqual(refusal["unmarked_count"], 1)
        self.assertEqual(refusal["total_count"], 1)
        self.assertEqual(self.delete_calls, [])

    def test_deletes_when_all_marked(self):
        review = {"id": 1, "node_id": "n1"}
        comments = [{"body": f"staged\n\n{ghreview.MARKER}"}]
        self._stub(review, comments)
        result, refusal = ghreview.clear_pending_guarded("o/r", 1, False)
        self.assertIsNone(refusal)
        self.assertEqual(result, {"deleted": 1, "forced": False})
        self.assertEqual(len(self.delete_calls), 1)
        self.assertIn("--method", self.delete_calls[0])
        self.assertIn("DELETE", self.delete_calls[0])

    def test_force_deletes_unmarked_and_reports_forced(self):
        review = {"id": 1, "node_id": "n1"}
        comments = [{"body": "hand drafted comment, no marker"}]
        self._stub(review, comments)
        result, refusal = ghreview.clear_pending_guarded("o/r", 1, True)
        self.assertIsNone(refusal)
        self.assertEqual(result, {"deleted": 1, "forced": True})
        self.assertEqual(len(self.delete_calls), 1)


if __name__ == "__main__":
    unittest.main()
