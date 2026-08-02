#!/usr/bin/env python3
"""Tests for scripts/ghreview.py."""
import contextlib
import io
import importlib.util
import json
import os
import subprocess
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GHREVIEW_PY = os.path.join(REPO, "plugins", "leo", "scripts", "ghreview.py")

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


class TestThreadQueryContract(unittest.TestCase):
    """Thread output must preserve GitHub's original anchor separately from
    the current anchor, including the null used for file-level comments."""

    def test_query_requests_original_line(self):
        self.assertIn("originalLine", ghreview.THREADS_QUERY)

    def test_file_level_and_outdated_line_threads_are_reported_without_gh(self):
        original_login = ghreview.current_login
        original_fetch = ghreview.fetch_threads
        ghreview.current_login = lambda: "leo"
        ghreview.fetch_threads = lambda repo, pr: [{
            "id": "PRRT_file", "isResolved": False, "isOutdated": False,
            "path": "src/a.py", "line": None, "originalLine": None,
            "comments": {"nodes": [{
                "author": {"login": "leo"}, "body": "file finding",
                "createdAt": "2026-01-01T00:00:00Z",
                "pullRequestReview": {"id": "r1", "state": "COMMENTED"},
            }]},
        }, {
            "id": "PRRT_outdated", "isResolved": False, "isOutdated": True,
            "path": "src/b.py", "line": None, "originalLine": 12,
            "comments": {"nodes": [{
                "author": {"login": "leo"}, "body": "old line finding",
                "createdAt": "2026-01-01T00:00:00Z",
                "pullRequestReview": {"id": "r2", "state": "COMMENTED"},
            }]},
        }]
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                ghreview.cmd_threads(types.SimpleNamespace(repo="o/r", pr=1, all=False))
        finally:
            ghreview.current_login = original_login
            ghreview.fetch_threads = original_fetch

        file_level, outdated = json.loads(output.getvalue())["threads"]
        self.assertIsNone(file_level["line"])
        self.assertIsNone(file_level["original_line"])
        self.assertFalse(file_level["is_outdated"])
        self.assertIsNone(outdated["line"])
        self.assertEqual(outdated["original_line"], 12)
        self.assertTrue(outdated["is_outdated"])


class TestParsePatch(unittest.TestCase):
    """parse_patch is the actual unified-diff parser — every other test in
    this file exercises snap_line/validate_comments against a hand-built
    diffmap via make_diffmap, so an off-by-one in parse_patch itself would
    silently attach review comments to the wrong line without any test
    noticing. These feed it literal GitHub-style `patch` strings."""

    def test_added_lines_addressable_on_right_only(self):
        patch = (
            "@@ -1,2 +1,3 @@\n"
            " context one\n"
            "+added line\n"
            " context two\n"
        )
        result = ghreview.parse_patch(patch)
        self.assertEqual(result["right"], {1, 2, 3})
        self.assertEqual(result["left"], set())
        self.assertEqual(result["hunks"], [{"r": (1, 3), "l": (1, 2)}])

    def test_deleted_lines_addressable_on_left_only(self):
        patch = (
            "@@ -1,3 +1,2 @@\n"
            " context one\n"
            "-removed line\n"
            " context two\n"
        )
        result = ghreview.parse_patch(patch)
        # Only the deleted line itself lands in `left` — context lines only
        # ever populate `right` (matching the GitHub review UI, which never
        # accepts LEFT-side comments on unchanged lines).
        self.assertEqual(result["left"], {2})
        self.assertEqual(result["right"], {1, 2})
        self.assertEqual(result["hunks"], [{"r": (1, 2), "l": (1, 3)}])

    def test_context_lines_addressable_on_right(self):
        patch = "@@ -5,3 +5,3 @@\n context a\n context b\n context c\n"
        result = ghreview.parse_patch(patch)
        self.assertEqual(result["right"], {5, 6, 7})
        self.assertEqual(result["left"], set())
        self.assertEqual(result["hunks"], [{"r": (5, 7), "l": (5, 7)}])

    def test_two_hunks_produce_two_independent_ranges(self):
        patch = (
            "@@ -1,2 +1,2 @@\n"
            " ctx1\n"
            "+add1\n"
            "@@ -50,2 +51,3 @@\n"
            " ctx2\n"
            "-del1\n"
            "-del2\n"
            "+add2\n"
        )
        result = ghreview.parse_patch(patch)
        self.assertEqual(result["hunks"], [
            {"r": (1, 2), "l": (1, 1)},
            {"r": (51, 52), "l": (50, 52)},
        ])
        self.assertEqual(result["right"], {1, 2, 51, 52})
        self.assertEqual(result["left"], {51, 52})

    def test_deletion_only_hunk_yields_inverted_right_range(self):
        # A hunk that only deletes lines (no context, no additions) never
        # advances new_ln past its starting value, so close_hunk() records
        # "r": (start, start - 1) — an inverted (empty-but-backwards) range.
        # This is today's actual behavior, not a spec; documenting it here
        # (rather than "fixing" it) so a future change to parse_patch has to
        # touch this test deliberately. Downstream, ranges()/snap_line() only
        # ever iterate the addressable-line sets, so the inverted tuple is
        # harmless in practice — but it is out of scope to change here.
        patch = "@@ -10,2 +10,0 @@\n-removed one\n-removed two\n"
        result = ghreview.parse_patch(patch)
        self.assertEqual(result["hunks"], [{"r": (10, 9), "l": (10, 11)}])
        self.assertEqual(result["left"], {10, 11})
        self.assertEqual(result["right"], set())


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

    def test_whitespace_only_unmarked_body_does_not_crash(self):
        # Regression for the IndexError: "   ".strip().splitlines() == [], so
        # indexing [0] used to raise instead of refusing cleanly.
        review = {"id": 1, "node_id": "n1"}
        comments = [{"body": "   "}]
        self._stub(review, comments)
        result, refusal = ghreview.clear_pending_guarded("o/r", 1, False)
        self.assertIsNone(result)
        self.assertTrue(refusal["refused"])
        self.assertEqual(refusal["samples"], [""])


class TestStageRetryRevalidation(unittest.TestCase):
    """Regression for the bogus-retry-report bug: on retry, cmd_stage used to
    revalidate the already-snapped `staged` entries instead of the original
    `comments`, so the reported "from" was the first pass's output line
    rather than the true original line — a fictitious second hop."""

    def setUp(self):
        self._orig = {
            name: getattr(ghreview, name)
            for name in ("fetch_files", "build_maps", "post_review", "gh")
        }

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(ghreview, name, fn)

    def test_retry_report_reflects_original_comment_line(self):
        # First pass: line 22 is off-diff and snaps to 20.
        map1 = {"a.py": make_diffmap([(10, 20, 10, 20)])}
        # Second pass (as if the head moved under us): only line 18 is
        # addressable now.
        map2 = {"a.py": make_sparse_diffmap((10, 20, 10, 20), [18])}
        maps_calls = [map1, map2]

        ghreview.fetch_files = lambda repo, pr: []
        ghreview.build_maps = lambda files: maps_calls.pop(0)
        ghreview.gh = lambda args, payload=None: "deadbeef"

        post_review_calls = {"n": 0}

        def fake_post_review(repo, pr, commit, staged):
            post_review_calls["n"] += 1
            if post_review_calls["n"] == 1:
                raise subprocess.CalledProcessError(1, ["gh"], "", "head moved")
            return {"id": 99, "state": "PENDING"}

        ghreview.post_review = fake_post_review

        comments = [{"path": "a.py", "line": 22, "side": "RIGHT", "body": "hi"}]
        args = types.SimpleNamespace(
            repo="o/r", pr=1, commit="orig-sha", input=None,
            dry_run=False, replace_pending=False, force=False,
        )

        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"comments": comments}, fh)
            args.input = fh.name
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                ghreview.cmd_stage(args)
        finally:
            os.unlink(args.input)

        report = json.loads(buf.getvalue())
        self.assertEqual(post_review_calls["n"], 2)
        # The retry's snap must be reported against the ORIGINAL line (22),
        # not against the first pass's already-snapped output (20).
        self.assertEqual(report["snapped"], [{"path": "a.py", "from": 22, "to": 18}])


if __name__ == "__main__":
    unittest.main()
