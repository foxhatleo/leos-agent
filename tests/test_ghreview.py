"""Behavioral tests for review staging and diff-line validation."""

import base64
import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import time
import unittest
from urllib.parse import quote, unquote
from pathlib import Path
import os
import tempfile
import types
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ghreview_test", ROOT / "scripts" / "ghreview.py")
ghreview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ghreview)

REAL_RUN = subprocess.run


def _no_real_gh(argv, *args, **kwargs):
    """Fail loudly instead of reaching the real gh: a test that forgets to
    stub GitHub would otherwise read or write a real account."""
    if argv and argv[0] == "gh":
        raise AssertionError("test reached the real gh: %r" % (argv,))
    return REAL_RUN(argv, *args, **kwargs)


_guard = mock.patch.object(subprocess, "run", _no_real_gh)


def setUpModule():
    _guard.start()


def tearDownModule():
    _guard.stop()


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
        payload = {"verdict": "seriously-problematic", "reviewed": ["a.py"],
                   "comments": [comment] * (ghreview.MAX_STAGE_COMMENTS + 1)}
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
        path.write_text(json.dumps({"verdict": "neutral", "reviewed": ["a.py"],
                                    "comments": [{"path": "a.py", "line": 1, "body": "finding"}]}))
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
                     "body": "x" * 2000, "reason": "r"} for i in range(60)]
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
        self.assertIn("y" * 50000, body)


class TestStageCoverage(unittest.TestCase):
    """cmd_stage's report is what watch_review trusts to close out a head."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.files = [{"filename": "a.py", "additions": 1, "deletions": 0,
                       "patch": "@@ -1,1 +1,1 @@\n+anchored\n"}]

    def stage(self, comments, verdict="seriously-problematic", reviewed=("a.py",), extra=None, **overrides):
        path = Path(self.tmp.name) / "in.json"
        path.write_text(json.dumps(dict({"comments": comments, "verdict": verdict,
                                         "reviewed": list(reviewed)}, **(extra or {}))))
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
             mock.patch.object(ghreview, "generated_rules", return_value=[]), \
             mock.patch.object(ghreview, "require_head"), \
             mock.patch.object(ghreview, "post_review", side_effect=fake_post), \
             mock.patch.object(ghreview, "review_comments", return_value=[]), \
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
        # ready-to-merge: seriously-problematic with nothing renderable is now
        # refused outright (see TestVerdictRules), which also creates nothing.
        report, posted, _ = self.stage([{"line": 5, "body": ""}], verdict="ready-to-merge")
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
        review = {"id": 7, "body": body}
        ghreview.remember_review("o/r", 1, review, comments, body)
        with mock.patch.object(ghreview, "pending_review", return_value=review), \
             mock.patch.object(ghreview, "review_comments", return_value=comments):
            snapshot, refusal = ghreview.pending_snapshot("o/r", 1)
        self.assertIsNone(refusal)
        self.assertFalse(snapshot["forced"])


def github_pending(comment_id, path, body, position, reply_to=None):
    """A comment as GET .../reviews/{id}/comments returns it for a PENDING review:
    no line, side, start_line, original_line or original_side; only positions."""
    return {"id": comment_id, "path": path, "body": body, "position": position,
            "original_position": position, "line": None, "side": None, "start_line": None,
            "start_side": None, "original_line": None, "original_side": None,
            "in_reply_to_id": reply_to}


class TestDraftOwnership(unittest.TestCase):
    """Every re-review of a draft this tool staged used to be refused as unowned:
    the receipt hashed the staged input's line/side, GitHub's pending re-read
    returns them as null, so the two fingerprints could never match."""

    SHA = "a" * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.files = [{"filename": "a.py", "status": "modified", "additions": 2, "deletions": 0,
                       "patch": "@@ -1,1 +1,3 @@\n ctx\n+one\n+two\n"}]
        self.inputs = [{"path": "a.py", "line": 2, "side": "RIGHT", "body": "first finding"},
                       {"path": "a.py", "line": 3, "side": "RIGHT", "body": "second finding"}]

    def stage(self, reread):
        """Stage self.inputs; `reread` is what GitHub returns when asked for the
        new review's comments (a list, or an exception)."""
        path = Path(self.tmp.name) / "in.json"
        path.write_text(json.dumps({"verdict": "neutral", "reviewed": ["a.py"], "comments": self.inputs}))
        args = types.SimpleNamespace(repo="o/r", pr=1, commit=self.SHA, input=str(path),
                                     replace_pending=False, force=False, dry_run=False)
        with mock.patch.object(ghreview, "pinned_files", return_value=self.files), \
             mock.patch.object(ghreview, "generated_rules", return_value=[]), \
             mock.patch.object(ghreview, "require_head"), \
             mock.patch.object(ghreview, "post_review",
                               return_value={"id": 70, "node_id": "R70", "state": "PENDING", "body": ""}), \
             mock.patch.object(ghreview, "review_comments",
                               **({"side_effect": reread} if isinstance(reread, Exception) else {"return_value": reread})), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ghreview.cmd_stage(args)

    def snapshot(self, comments, body="", force=False):
        review = {"id": 70, "node_id": "R70", "body": body, "commit_id": self.SHA}
        with mock.patch.object(ghreview, "pending_review", return_value=review), \
             mock.patch.object(ghreview, "review_comments", return_value=comments):
            return ghreview.pending_snapshot("o/r", 1, force)

    def github_view(self):
        return [github_pending(1, "a.py", ghreview._mark("first finding"), 2),
                github_pending(2, "a.py", ghreview._mark("second finding"), 3)]

    def test_a_github_shaped_pending_reread_is_recognised_as_owned(self):
        self.stage(self.github_view())
        snapshot, refusal = self.snapshot(self.github_view())
        self.assertIsNone(refusal)
        self.assertFalse(snapshot["forced"])

    def test_the_staged_input_is_an_equivalent_receipt_when_the_reread_fails(self):
        import subprocess
        self.stage(subprocess.CalledProcessError(1, ["gh"], "", "HTTP 502"))
        snapshot, refusal = self.snapshot(self.github_view())
        self.assertIsNone(refusal)

    def test_the_same_reread_with_one_body_edited_is_refused(self):
        self.stage(self.github_view())
        edited = self.github_view()
        edited[1]["body"] = ghreview._mark("second finding, reworded by hand")
        snapshot, refusal = self.snapshot(edited)
        self.assertIsNone(snapshot)
        self.assertIn("edited", refusal["reason"])
        self.assertEqual(refusal["changed_paths"], ["a.py"])

    def test_an_added_or_removed_comment_is_refused(self):
        self.stage(self.github_view())
        added = self.github_view() + [github_pending(3, "b.py", "my own note", 1)]
        self.assertIn("edited", self.snapshot(added)[1]["reason"])
        self.assertIn("edited", self.snapshot(self.github_view()[:1])[1]["reason"])

    def test_an_edited_review_body_is_refused(self):
        self.stage(self.github_view())
        self.assertTrue(self.snapshot(self.github_view(), body="added a summary")[1]["refused"])

    def test_a_receipt_from_the_old_scheme_asks_for_a_one_time_force(self):
        receipt = ghreview.receipt_path("o/r", 1, 70)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"fingerprint": "0" * 64, "review_id": 70}))
        snapshot, refusal = self.snapshot(self.github_view())
        self.assertTrue(refusal["legacy_receipt"])
        self.assertIn("--force", refusal["reason"])
        snapshot, refusal = self.snapshot(self.github_view(), force=True)
        self.assertIsNone(refusal)
        self.assertTrue(snapshot["forced"])

    def test_restoration_posts_a_position_when_github_gave_no_line(self):
        pending = github_pending(1, "a.py", "body", 4)
        self.assertEqual(ghreview.restore_anchor(pending), {"path": "a.py", "body": "body", "position": 4})
        anchored = {"path": "a.py", "body": "b", "line": 9, "side": "RIGHT", "position": 4}
        self.assertEqual(ghreview.restore_anchor(anchored),
                         {"path": "a.py", "body": "b", "line": 9, "side": "RIGHT"})

    def test_our_own_reply_is_named_as_a_reply_not_as_an_edit(self):
        self.stage(self.github_view())
        body = Path(self.tmp.name) / "reply.txt"
        body.write_text("Addressed?")
        with_reply = self.github_view() + [github_pending(3, "a.py", ghreview._mark("Addressed?"), 2, reply_to=55)]
        review = {"id": 70, "node_id": "R70", "body": "", "commit_id": self.SHA}
        args = types.SimpleNamespace(repo="o/r", pr=1, commit=self.SHA, thread_id="T",
                                     body_file=str(body), dry_run=False)
        with mock.patch.object(ghreview, "require_thread"), \
             mock.patch.object(ghreview, "require_head"), \
             mock.patch.object(ghreview, "pending_review", return_value=review), \
             mock.patch.object(ghreview, "review_comments", side_effect=[self.github_view(), with_reply]), \
             mock.patch.object(ghreview, "graphql", return_value={"data": {
                 "addPullRequestReviewThreadReply": {"comment": {"id": "C"}}}}), \
             contextlib.redirect_stdout(io.StringIO()):
            ghreview.cmd_reply(args)
        # Our own reply keeps the draft owned and, with its thread known, recoverable.
        thread = {"id": "T", "comments": {"nodes": [
            {"databaseId": 55, "pullRequestReview": {"state": "COMMENTED"}}]}}
        with mock.patch.object(ghreview, "fetch_threads", return_value=[thread]):
            snapshot, refusal = self.snapshot(with_reply)
        self.assertIsNone(refusal)
        self.assertEqual(snapshot["replies"][0]["thread_id"], "T")
        # Without a thread to put it back on, it is preserved, and named as a reply.
        with mock.patch.object(ghreview, "fetch_threads", return_value=[]):
            _, refusal = self.snapshot(with_reply)
        self.assertIn("reply", refusal["reason"])
        self.assertNotIn("edited", refusal["reason"])


class TestVerdictRules(unittest.TestCase):
    SHA = "a" * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.files = [
            {"filename": "a.py", "status": "modified", "patch": "@@ -1,1 +1,2 @@\n ctx\n+new\n"},
            {"filename": "tests/test_a.py", "status": "added", "patch": "@@ -0,0 +1,1 @@\n+test\n"},
            {"filename": "package-lock.json", "status": "modified", "patch": "@@ -1,1 +1,1 @@\n-x\n+y\n"},
        ]

    def stage(self, verdict, comments=(), notes=(), reviewed=("a.py", "tests/test_a.py"), override=None):
        data = {"verdict": verdict, "reviewed": list(reviewed), "comments": list(comments), "notes": list(notes)}
        if override is not None:
            data["verdict_override"] = override
        path = Path(self.tmp.name) / "in.json"
        path.write_text(json.dumps(data))
        args = types.SimpleNamespace(repo="o/r", pr=1, commit=self.SHA, input=str(path),
                                     replace_pending=False, force=False, dry_run=False)
        posted = {}

        def fake_post(repo, pr, commit, staged, body=""):
            posted.update(staged=staged, body=body)
            return {"id": 7, "node_id": "n", "state": "PENDING", "body": body}

        out = io.StringIO()
        with mock.patch.object(ghreview, "pinned_files", return_value=self.files), \
             mock.patch.object(ghreview, "generated_rules", return_value=[]), \
             mock.patch.object(ghreview, "require_head"), \
             mock.patch.object(ghreview, "post_review", side_effect=fake_post), \
             mock.patch.object(ghreview, "review_comments", return_value=[]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            ghreview.cmd_stage(args)
        return json.loads(out.getvalue()), posted

    def test_neutral_with_nothing_pending_is_refused_before_any_mutation(self):
        with mock.patch.object(ghreview, "post_review") as post:
            with self.assertRaisesRegex(ValueError, "neutral needs at least one pending comment"):
                self.stage("neutral")
            post.assert_not_called()
        self.assertIsNone(ghreview.load_verdict("o/r", 1))

    def test_a_reservation_staged_as_a_note_satisfies_neutral(self):
        report, posted = self.stage("neutral", notes=["Could not read LIN-12; acceptance criteria unchecked."])
        self.assertEqual((report["notes"], report["review_created"]), (1, True))
        self.assertIn("LIN-12", posted["body"])
        self.assertIn(ghreview.MARKER, posted["body"])

    def test_ready_with_nothing_to_say_creates_no_review_and_is_recorded(self):
        report, posted = self.stage("ready-to-merge")
        self.assertEqual(posted, {})
        self.assertTrue(report["complete"])
        self.assertEqual(ghreview.load_verdict("o/r", 1)["verdict"], "ready-to-merge")
        self.assertEqual(ghreview.load_verdict("o/r", 1)["head"], self.SHA)

    def test_coverage_names_reviewed_generated_and_unreviewed_files(self):
        report, _ = self.stage("seriously-problematic", reviewed=["a.py"],
                               comments=[{"path": "a.py", "line": 2, "body": "breaks"}])
        self.assertEqual(report["coverage"], {
            "files_changed": 3, "files_reviewed": 1, "files_skipped_generated": ["package-lock.json"],
            "unreviewed": ["tests/test_a.py"], "complete": False})

    def test_ready_with_an_unread_test_file_is_refused(self):
        with self.assertRaisesRegex(ValueError, "tests/test_a.py"):
            self.stage("ready-to-merge", reviewed=["a.py"])

    def test_a_standing_ready_verdict_is_not_downgraded_without_an_override(self):
        self.stage("ready-to-merge")
        # A later pass that, say, could not read the ticket must not quietly undo it.
        with self.assertRaisesRegex(ValueError, "stands"):
            self.stage("neutral", notes=["Ticket not read on this pass."])
        self.assertEqual(ghreview.load_verdict("o/r", 1)["verdict"], "ready-to-merge")
        report, _ = self.stage("seriously-problematic", override="new: a.py:2 drops the retry",
                               comments=[{"path": "a.py", "line": 2, "body": "drops the retry"}])
        self.assertEqual(report["verdict_override"], "new: a.py:2 drops the retry")
        self.assertEqual(report["prior_verdict"]["verdict"], "ready-to-merge")
        self.assertEqual(ghreview.load_verdict("o/r", 1)["override"], "new: a.py:2 drops the retry")

    def test_a_changed_pr_diff_makes_the_ready_verdict_stale(self):
        self.stage("ready-to-merge")
        self.files[0]["patch"] += "+another line\n"
        report, _ = self.stage("neutral", comments=[{"path": "a.py", "line": 2, "body": "question"}])
        self.assertEqual(report["verdict"], "neutral")
        self.assertNotIn("verdict_override", report)

    def test_a_missing_verdict_is_an_input_error(self):
        with self.assertRaisesRegex(ValueError, "needs a verdict"):
            ghreview.stage_request({"comments": []})


class TestDiffFingerprint(unittest.TestCase):
    def files(self, header, line="+new"):
        return [{"filename": "a.py", "status": "modified", "patch": "%s def f():\n ctx\n%s\n" % (header, line)}]

    def test_a_merge_from_the_base_that_only_shifts_lines_is_the_same_change(self):
        self.assertEqual(ghreview.diff_fingerprint(self.files("@@ -10,1 +10,2 @@")),
                         ghreview.diff_fingerprint(self.files("@@ -40,1 +40,2 @@")))

    def test_new_commits_on_the_branch_are_a_different_change(self):
        self.assertNotEqual(ghreview.diff_fingerprint(self.files("@@ -10,1 +10,2 @@")),
                            ghreview.diff_fingerprint(self.files("@@ -10,1 +10,2 @@", "+newer")))

    def test_a_file_without_a_patch_is_identified_by_its_blob(self):
        a = [{"filename": "img.png", "status": "modified", "sha": "1"}]
        b = [{"filename": "img.png", "status": "modified", "sha": "2"}]
        self.assertNotEqual(ghreview.diff_fingerprint(a), ghreview.diff_fingerprint(b))


class NotFound(Exception):
    pass


class FakeGitHub:
    """GitHub as the gh CLI presents it to ghreview.

    Installed in place of subprocess.run, so gh() itself runs: its argument
    shapes, timeouts, retries and NDJSON splitting are all exercised. It keeps
    real GitHub's behaviour where ghreview depends on it: owner/repo match
    case-insensitively and full_name returns the canonical spelling; `--jq
    '.[]'` prints one compact object per line with non-ASCII unescaped, as jq
    does; a pending review re-reads with null line/side and only a position;
    one pending review per user; contents/ returns base64 up to 1 MB and
    encoding "none" above it, and raw bytes for the raw media type; a missing
    path is `gh: Not Found (HTTP 404)` on stderr.
    """

    def __init__(self, head, files, full_name="Owner/Repo", base="c" * 40, login="me"):
        self.heads = [head]
        self.files = files
        self.full_name = full_name
        self.base = base
        self.login = login
        self.blobs = {}  # (sha, path) -> bytes
        self.dirs = {}   # (sha, path) -> [entry]
        self.reviews, self.comments, self.next_id = {}, {}, 100
        self.next_comment = 10_000
        # Submitted threads: {"id", "path", "line", "root_id" (REST id), "author", "body"}.
        self.threads = []
        # Heads the next file listings describe; empty means the current head.
        # GitHub can serve the previous head's listing just after a push.
        self.listing_refs = []
        self.failures = []  # [(predicate(args), stderr)], each consumed once
        self.calls = []

    def head(self):
        return self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]

    def run(self, argv, input=None, capture_output=False, text=False, timeout=None, **kwargs):
        if not argv or argv[0] != "gh":
            return REAL_RUN(argv, input=input, capture_output=capture_output, text=text,
                            timeout=timeout, **kwargs)
        args = list(argv[1:])
        self.calls.append({"args": args, "timeout": timeout})
        for i, (matches, stderr) in enumerate(self.failures):
            if matches(args):
                del self.failures[i]
                return self.done(argv, 1, "", stderr, text)
        try:
            out = self.handle(args, input)
        except NotFound:
            return self.done(argv, 1, "", "gh: Not Found (HTTP 404)\n", text)
        except subprocess.CalledProcessError as error:
            return self.done(argv, 1, "", error.stderr, text)
        return self.done(argv, 0, out, "", text)

    @staticmethod
    def done(argv, code, out, err, text):
        if text:
            out = out.decode() if isinstance(out, bytes) else out
        else:
            out = out if isinstance(out, bytes) else out.encode()
            err = err.encode()
        return subprocess.CompletedProcess(argv, code, out, err)

    @staticmethod
    def lines(objects):
        return "".join(json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n" for o in objects)

    def repo_path(self, route):
        match = re.fullmatch(r"repos/([^/]+)/([^/]+)(/.*)?", route)
        if not match or ("%s/%s" % match.group(1, 2)).lower() != self.full_name.lower():
            raise NotFound()
        return match.group(3) or ""

    def handle(self, args, payload):
        assert args[0] == "api", args
        jq = method = None
        headers, rest, fields, i = [], [], {}, 1
        while i < len(args):
            if args[i] in ("-f", "-F"):
                key, _, value = args[i + 1].partition("=")
                fields[key] = value
                i += 2
            elif args[i] == "-H":
                headers.append(args[i + 1])
                i += 2
            elif args[i] in ("--jq", "-q"):
                jq = args[i + 1]
                i += 2
            elif args[i] == "--method":
                method = args[i + 1]
                i += 2
            elif args[i] == "--input":
                i += 2
            else:
                rest.append(args[i])
                i += 1
        route, _, query = [p for p in rest if p != "--paginate"][0].partition("?")
        if route == "user":
            return self.login + "\n"
        if route == "graphql":
            return self.graphql(fields)
        sub = self.repo_path(route)
        if sub == "":
            assert jq == ".full_name"
            return self.full_name + "\n"
        if re.fullmatch(r"/pulls/\d+", sub):
            return (self.head() if jq == ".head.sha" else self.base) + "\n"
        if re.fullmatch(r"/pulls/\d+/files", sub):
            return self.listing()
        if re.fullmatch(r"/pulls/\d+/reviews", sub):
            if method == "POST":
                return self.post_review(json.loads(payload))
            return self.lines(self.reviews.values())
        match = re.fullmatch(r"/pulls/\d+/reviews/(\d+)/comments", sub)
        if match:
            return self.lines(self.comments.get(int(match.group(1)), []))
        match = re.fullmatch(r"/pulls/\d+/reviews/(\d+)", sub)
        if match and method == "DELETE":
            rid = int(match.group(1))
            self.reviews.pop(rid)
            self.comments.pop(rid, None)
            return "{}"
        match = re.fullmatch(r"/contents/?(.*)", sub)
        if match:
            ref = dict(p.split("=", 1) for p in query.split("&") if p)["ref"]
            return self.content(unquote(match.group(1)), ref, raw=any("raw" in h for h in headers))
        raise AssertionError("unhandled gh call %r" % (args,))

    def listing(self):
        """Each entry's contents_url names the head the listing describes; a
        removed file's names the base side, as GitHub's does."""
        ref = self.listing_refs.pop(0) if self.listing_refs else self.heads[0]
        entries = []
        for f in self.files:
            f = dict(f)
            side = self.base if f.get("status") == "removed" else ref
            f.setdefault("contents_url", "https://api.github.com/repos/%s/contents/%s?ref=%s"
                         % (self.full_name, quote(f["filename"], safe=""), side))
            entries.append(f)
        return self.lines(entries)

    def graphql(self, fields):
        query = fields["query"]
        if "addPullRequestReviewThreadReply" in query:
            review = next((r for r in self.reviews.values()
                           if r["node_id"] == fields["review"] and r["state"] == "PENDING"), None)
            thread = next((t for t in self.threads if t["id"] == fields["thread"]), None)
            if review is None or thread is None:
                raise subprocess.CalledProcessError(1, ["gh"], "", "GraphQL: Could not resolve to a node\n")
            self.next_comment += 1
            self.comments[review["id"]].append({
                "id": self.next_comment, "path": thread["path"], "body": fields["body"], "position": None,
                "line": None, "side": None, "start_line": None, "in_reply_to_id": thread["root_id"]})
            return json.dumps({"data": {"addPullRequestReviewThreadReply": {
                "comment": {"id": "PRRC_%d" % self.next_comment}}}})
        assert "reviewThreads" in query, query
        nodes = [{"id": t["id"], "isResolved": False, "isOutdated": False, "path": t["path"],
                  "line": t["line"], "originalLine": t["line"],
                  "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{
                      "id": "PRRC_root_%s" % t["id"], "databaseId": t["root_id"],
                      "author": {"login": t["author"]}, "body": t["body"], "createdAt": "2026-01-01T00:00:00Z",
                      "pullRequestReview": {"id": "PRR_submitted", "state": "COMMENTED"}}]}}
                 for t in self.threads]
        return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}})

    def post_review(self, body):
        if any(r["state"] == "PENDING" for r in self.reviews.values()):
            raise subprocess.CalledProcessError(
                1, ["gh"], "", "HTTP 422: User can only have one pending review per pull request")
        rid = self.next_id
        self.next_id += 1
        self.reviews[rid] = {"id": rid, "node_id": "R%d" % rid, "state": "PENDING",
                             "user": {"login": self.login}, "body": body.get("body") or "",
                             "commit_id": body["commit_id"]}
        self.comments[rid] = [{"id": rid * 10 + n, "path": c["path"], "body": c["body"],
                               "position": c.get("position", c.get("line")), "line": None, "side": None,
                               "start_line": None, "in_reply_to_id": None}
                              for n, c in enumerate(body.get("comments") or [])]
        return json.dumps(self.reviews[rid])

    def content(self, path, ref, raw):
        if (ref, path) in self.dirs:
            return json.dumps(self.dirs[(ref, path)])
        if (ref, path) not in self.blobs:
            raise NotFound()
        data = self.blobs[(ref, path)]
        if raw:
            return data
        meta = {"type": "file", "path": path, "size": len(data), "sha": "blob"}
        if len(data) <= 1_000_000:
            encoded = base64.b64encode(data).decode()
            meta.update(encoding="base64",
                        content="\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60)))
        else:
            meta.update(encoding="none", content="")
        return json.dumps(meta)


class GitHubCase(unittest.TestCase):
    """Runs ghreview.main() against FakeGitHub with a private data root."""

    H1, H2 = "1" * 40, "2" * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        ghreview.GIVEN_SPELLING.clear()
        patcher = mock.patch.object(ghreview, "RETRY_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def github(self, head, files, **kwargs):
        fake = FakeGitHub(head, files, **kwargs)
        patcher = mock.patch.object(subprocess, "run", fake.run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def main(self, *argv):
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", ["ghreview.py"] + list(argv)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ghreview.main()
            except SystemExit as exit_:
                code = exit_.code or 0
        return code, out.getvalue(), err.getvalue()

    def stage(self, repo, data, head, *extra):
        path = Path(self.tmp.name) / "in.json"
        path.write_text(json.dumps(data))
        code, out, err = self.main("stage", "-R", repo, "-n", "1", "--commit", head,
                                   "--input", str(path), *extra)
        return code, (json.loads(out) if code == 0 and out.strip() else None), err


def text_file(name, patch, status="modified", sha="blob"):
    adds = sum(1 for line in patch.split("\n") if line.startswith("+"))
    dels = sum(1 for line in patch.split("\n") if line.startswith("-"))
    return {"filename": name, "status": status, "additions": adds, "deletions": dels, "sha": sha, "patch": patch}


def first_lines(fake):
    return [c["body"].split("\n")[0] for cs in fake.comments.values() for c in cs]


class TestLineSeparators(unittest.TestCase):
    """git counts lines by newline only; str.splitlines() did not."""

    def test_separator_characters_inside_a_line_do_not_shift_line_numbers(self):
        for char in ("\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\x85", " ", " ", "\r"):
            with self.subTest(char=repr(char)):
                parsed = ghreview.parse_patch("@@ -10,4 +10,5 @@\n a\n b%s c\n+new\n d\n e" % char)
                self.assertEqual(parsed["right"], {10, 11, 12, 13, 14})
                self.assertEqual(parsed["hunks"], [{"r": (10, 14), "l": (10, 13)}])

    def test_a_trailing_newline_ends_the_last_line_rather_than_adding_one(self):
        self.assertEqual(ghreview.parse_patch("@@ -1,1 +1,2 @@\n ctx\n+new\n")["right"], {1, 2})

    def test_crlf_lines_are_still_one_line_each(self):
        parsed = ghreview.parse_patch("@@ -1,2 +1,2 @@\n ctx\r\n-old\r\n+new\r\n")
        self.assertEqual((parsed["right"], parsed["left"]), ({1, 2}, {2}))

    def test_the_fingerprint_of_an_ordinary_patch_is_what_older_releases_stored(self):
        """Stored ready-to-merge verdicts must keep standing across this fix."""
        files = [text_file("a.py", "@@ -3,2 +3,3 @@\n ctx\n+new\n tail"),
                 text_file("b.py", "@@ -1 +1 @@\n-x\n+y\n")]
        rows = [[f["filename"], None, f["status"],
                 "\n".join("@@" if ghreview.HUNK_RE.match(l) else l for l in f["patch"].splitlines())]
                for f in files]
        old = ghreview.hashlib.sha256(json.dumps(rows).encode()).hexdigest()
        self.assertEqual(ghreview.diff_fingerprint(files), old)

    def test_a_form_feed_turned_into_a_newline_is_a_different_change(self):
        a = [text_file("a.py", "@@ -1 +1 @@\n+x\x0cy")]
        b = [text_file("a.py", "@@ -1 +1,2 @@\n+x\n+y")]
        self.assertNotEqual(ghreview.diff_fingerprint(a), ghreview.diff_fingerprint(b))


class TestMultiLineRanges(unittest.TestCase):
    PATCH = "@@ -1,3 +1,4 @@\n a\n+b\n c\n d\n@@ -40,3 +41,4 @@\n x\n+y\n z\n w\n"

    def test_a_range_across_two_hunks_is_staged_on_its_last_line_only(self):
        narrowed = []
        staged, _, dropped = ghreview.validate_comments(
            [{"path": "f.py", "start_line": 2, "line": 42, "side": "RIGHT", "body": "spans hunks"}],
            {"f.py": ghreview.parse_patch(self.PATCH)}, narrowed)
        self.assertEqual(dropped, [])
        self.assertNotIn("start_line", staged[0])
        self.assertEqual(staged[0]["line"], 42)
        self.assertEqual(narrowed[0]["start_line"], 2)

    def test_a_range_inside_one_hunk_is_kept(self):
        staged, _, _ = ghreview.validate_comments(
            [{"path": "f.py", "start_line": 41, "line": 43, "side": "RIGHT", "body": "one hunk"}],
            {"f.py": ghreview.parse_patch(self.PATCH)})
        self.assertEqual((staged[0]["start_line"], staged[0]["line"]), (41, 43))


class TestGeneratedFiles(unittest.TestCase):
    def test_hand_written_code_in_build_dist_or_vendor_directories_is_reviewed(self):
        for path in ("build/gulpfile.js", "src/build/compiler.ts", "vendor/patched/lib.go",
                     "dist/cli.py", "tests/__snapshots__/view.test.js.snap", "maps/level1.map"):
            with self.subTest(path=path):
                self.assertFalse(ghreview.is_generated(path))

    def test_lockfiles_and_generated_suffixes_are_exempt(self):
        for path in ("package-lock.json", "web/yarn.lock", "go.sum", "app.min.js", "app.js.map",
                     "api/service.pb.go", "proto/thing_pb2.py", "schema.generated.ts"):
            with self.subTest(path=path):
                self.assertTrue(ghreview.is_generated(path))

    def test_gitattributes_marks_and_unmarks_with_the_last_matching_line_winning(self):
        rules = ghreview.attribute_rules(
            "# comment\n"
            "dist/** -diff linguist-generated=true\n"
            "dist/keep.js linguist-generated=false\n"
            "*.snap linguist-generated\n"
            "package-lock.json -linguist-generated\n"
            "/gen/*.ts linguist-generated\n"
            "**/fixtures/** linguist-generated\n"
            "docs/ linguist-generated\n")
        expected = {"dist/index.js": True, "dist/keep.js": False, "src/a/view.snap": True,
                    "package-lock.json": False, "gen/a.ts": True, "src/gen/a.ts": False,
                    "a/b/fixtures/c.json": True, "docs/readme.md": False, "src/app.ts": False}
        for path, generated in expected.items():
            with self.subTest(path=path):
                self.assertEqual(ghreview.is_generated(path, rules), generated)


class TestStageAgainstGitHub(GitHubCase):
    FILES = [text_file("a.py", "@@ -1,1 +1,3 @@\n ctx\n+one\n+two"),
             text_file("dist/bundle.js", "@@ -1 +1 @@\n-x\n+y"),
             text_file("src/core.py", "@@ -1 +1 @@\n-p\n+q")]
    READ = ["a.py", "dist/bundle.js", "src/core.py"]

    def test_the_base_branchs_gitattributes_decides_generated_not_the_heads(self):
        fake = self.github(self.H1, self.FILES)
        fake.blobs[(fake.base, ".gitattributes")] = b"dist/** linguist-generated=true\n"
        # The PR's own head tries to exempt everything it changes from review.
        fake.blobs[(self.H1, ".gitattributes")] = b"** linguist-generated=true\n"
        code, report, err = self.stage("Owner/Repo", {"verdict": "neutral", "reviewed": ["a.py"],
                                                      "notes": ["question"]}, self.H1)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["coverage"]["files_skipped_generated"], ["dist/bundle.js"])
        self.assertEqual(report["coverage"]["unreviewed"], ["src/core.py"])

    def test_seriously_problematic_with_nothing_staged_is_refused_before_any_mutation(self):
        fake = self.github(self.H1, self.FILES)
        code, _, err = self.stage("Owner/Repo", {"verdict": "seriously-problematic", "reviewed": []}, self.H1)
        self.assertEqual(code, 2)
        self.assertIn("seriously-problematic needs", err)
        self.assertFalse(any("POST" in c["args"] for c in fake.calls))
        self.assertIsNone(ghreview.load_verdict("Owner/Repo", 1))

    def test_the_example_override_text_is_refused(self):
        self.github(self.H1, self.FILES)
        ghreview.save_verdict("Owner/Repo", 1, {"verdict": "ready-to-merge", "head": self.H1, "at": 1,
                                                "diff": ghreview.diff_fingerprint(self.FILES)})
        data = {"verdict": "neutral", "reviewed": self.READ, "notes": ["n"],
                "verdict_override": "only when overriding a standing ready-to-merge: the new defect"}
        code, _, err = self.stage("Owner/Repo", data, self.H1)
        self.assertEqual(code, 2)
        self.assertIn("template", err)
        self.assertEqual(ghreview.load_verdict("Owner/Repo", 1)["verdict"], "ready-to-merge")
        data["verdict_override"] = "a.py:2 drops the retry on timeout"
        code, report, err = self.stage("Owner/Repo", data, self.H1)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["verdict_override"], "a.py:2 drops the retry on timeout")

    def test_a_low_confidence_comment_is_filtered_not_posted(self):
        fake = self.github(self.H1, self.FILES)
        data = {"verdict": "neutral", "reviewed": self.READ,
                "comments": [{"path": "a.py", "line": 2, "body": "maybe", "confidence": 60},
                             {"path": "a.py", "line": 3, "body": "real", "confidence": 90}]}
        code, report, err = self.stage("Owner/Repo", data, self.H1)
        self.assertEqual(code, 0, err)
        self.assertEqual((report["staged"], len(report["filtered"]), report["complete"]), (1, 1, True))
        self.assertEqual(first_lines(fake), ["real"])
        # Only a filtered finding left: neutral has nothing pending, so it is refused.
        data["comments"] = data["comments"][:1]
        self.assertEqual(self.stage("Owner/Repo", data, self.H1, "--replace-pending")[0], 2)
        data["comments"][0]["confidence"] = "high"
        self.assertEqual(self.stage("Owner/Repo", data, self.H1, "--replace-pending")[0], 2)

    def test_repo_spelling_does_not_change_ownership_or_the_standing_verdict(self):
        fake = self.github(self.H1, self.FILES, full_name="Foo/Bar")
        first = {"verdict": "neutral", "reviewed": self.READ,
                 "comments": [{"path": "a.py", "line": 2, "body": "first"}]}
        self.assertEqual(self.stage("Foo/Bar", first, self.H1)[0], 0)
        again = dict(first, comments=[{"path": "a.py", "line": 3, "body": "second"}])
        code, report, err = self.stage("foo/bar", again, self.H1, "--replace-pending")
        self.assertEqual(code, 0, err)
        self.assertEqual(report["repo"], "Foo/Bar")
        self.assertEqual(first_lines(fake), ["second"])
        self.assertEqual(ghreview.load_verdict("FOO/BAR", 1)["verdict"], "neutral")

    def test_a_receipt_and_a_verdict_stored_under_an_old_spelling_are_still_found(self):
        fake = self.github(self.H1, self.FILES, full_name="Foo/Bar")
        fake.post_review({"commit_id": self.H1, "body": "",
                          "comments": [{"path": "a.py", "line": 2, "body": ghreview._mark("old")}]})
        rid = max(fake.reviews)
        # An older release keyed both by the spelling it was given.
        ghreview.remember_review("foo/bar", 1, fake.reviews[rid], fake.comments[rid])
        ghreview.save_verdict("foo/bar", 1, {"verdict": "neutral", "head": self.H1, "diff": "d", "at": 5})
        self.assertEqual(ghreview.load_verdict("Foo/Bar", 1)["at"], 5)
        data = {"verdict": "neutral", "reviewed": self.READ,
                "comments": [{"path": "a.py", "line": 3, "body": "new"}]}
        code, report, err = self.stage("foo/bar", data, self.H1, "--replace-pending")
        self.assertEqual(code, 0, err)
        self.assertEqual(report["deleted_pending"], rid)
        self.assertEqual(first_lines(fake), ["new"])

    def test_no_findings_replacement_checks_the_head_before_deleting_the_draft(self):
        fake = self.github(self.H1, self.FILES)
        data = {"verdict": "neutral", "reviewed": self.READ,
                "comments": [{"path": "a.py", "line": 2, "body": "valid finding"}]}
        self.assertEqual(self.stage("Owner/Repo", data, self.H1)[0], 0)
        draft = set(fake.reviews)
        # Two head reads for the file list, then the head moves.
        fake.heads = [self.H1, self.H1, self.H2]
        code, _, err = self.stage("Owner/Repo", {"verdict": "ready-to-merge", "reviewed": self.READ},
                                  self.H1, "--replace-pending")
        self.assertEqual(code, 2)
        self.assertIn("head changed", err)
        self.assertEqual(set(fake.reviews), draft)
        self.assertEqual(ghreview.load_verdict("Owner/Repo", 1)["verdict"], "neutral")

    def test_a_head_that_moves_after_the_deletion_restores_the_draft(self):
        fake = self.github(self.H1, self.FILES)
        data = {"verdict": "neutral", "reviewed": self.READ,
                "comments": [{"path": "a.py", "line": 2, "body": "valid finding"}]}
        self.assertEqual(self.stage("Owner/Repo", data, self.H1)[0], 0)
        fake.heads = [self.H1, self.H1, self.H1, self.H2]
        code, _, err = self.stage("Owner/Repo", {"verdict": "ready-to-merge", "reviewed": self.READ},
                                  self.H1, "--replace-pending")
        self.assertEqual(code, 2)
        self.assertIn("head changed", err)
        self.assertIn("recoverable", err)
        self.assertEqual(first_lines(fake), ["valid finding"])
        self.assertEqual(ghreview.load_verdict("Owner/Repo", 1)["verdict"], "neutral")


class TestIncrementalReview(GitHubCase):
    A1 = text_file("a.py", "@@ -1,1 +1,2 @@\n ctx\n+one")
    B1 = text_file("b.py", "@@ -10,1 +10,2 @@\n ctx\n+keep")
    # At the next head a.py's change grew; b.py's patch only moved (a base merge).
    A2 = text_file("a.py", "@@ -1,1 +1,3 @@\n ctx\n+one\n+two")
    B2 = text_file("b.py", "@@ -30,1 +30,2 @@\n ctx\n+keep")

    def review_first_head(self, fake):
        code, _, err = self.stage("Owner/Repo", {"verdict": "ready-to-merge", "reviewed": ["a.py", "b.py"]},
                                  self.H1)
        self.assertEqual(code, 0, err)
        fake.heads, fake.files = [self.H2], [self.A2, self.B2]

    def test_only_files_whose_patch_changed_must_be_read_again(self):
        fake = self.github(self.H1, [self.A1, self.B1])
        self.review_first_head(fake)
        code, out, err = self.main("verdict", "-R", "Owner/Repo", "-n", "1", "--commit", self.H2)
        self.assertEqual(json.loads(out)["incremental_since"], self.H1)
        code, out, err = self.main("delta", "-R", "Owner/Repo", "-n", "1", "--commit", self.H2, "--since", self.H1)
        plan = json.loads(out)
        self.assertEqual((plan["incremental"], plan["review"], plan["carry"]), (True, ["a.py"], ["b.py"]))
        code, report, err = self.stage("Owner/Repo", {"verdict": "ready-to-merge", "reviewed": ["a.py"]},
                                       self.H2, "--since", self.H1)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["coverage"]["carried"], ["b.py"])
        self.assertEqual(report["coverage"]["carried_from"], self.H1)
        record = ghreview.load_verdict("Owner/Repo", 1)
        self.assertEqual((record["carried_from"], sorted(record["patches"])), (self.H1, ["a.py", "b.py"]))

    def test_a_changed_file_that_was_not_read_again_blocks_ready(self):
        fake = self.github(self.H1, [self.A1, self.B1])
        self.review_first_head(fake)
        code, _, err = self.stage("Owner/Repo", {"verdict": "ready-to-merge", "reviewed": ["b.py"]},
                                  self.H2, "--since", self.H1)
        self.assertEqual(code, 2)
        self.assertIn("unreviewed: a.py", err)

    def test_since_without_a_recorded_review_at_that_head_is_refused(self):
        self.github(self.H2, [self.A2, self.B2])
        code, _, err = self.stage("Owner/Repo", {"verdict": "ready-to-merge", "reviewed": ["a.py"]},
                                  self.H2, "--since", self.H1)
        self.assertEqual(code, 2)
        self.assertIn("no review with per-file coverage", err)

    def test_a_pending_draft_with_earlier_findings_prevents_carrying(self):
        fake = self.github(self.H1, [self.A1, self.B1])
        code, _, err = self.stage("Owner/Repo", {"verdict": "neutral", "reviewed": ["a.py", "b.py"],
                                                 "comments": [{"path": "b.py", "line": 11, "body": "keep?"}]},
                                  self.H1)
        self.assertEqual(code, 0, err)
        fake.heads, fake.files = [self.H2], [self.A2, self.B2]
        code, _, err = self.stage("Owner/Repo", {"verdict": "neutral", "reviewed": ["a.py"], "notes": ["n"]},
                                  self.H2, "--since", self.H1, "--replace-pending")
        self.assertEqual(code, 2)
        self.assertIn("pending draft", err)
        self.assertEqual(first_lines(fake), ["keep?"])


class TestShowAtSha(GitHubCase):
    def show(self, *argv):
        return self.main("show", "-R", "owner/repo", "-n", "1", *argv)

    def test_a_file_is_numbered_as_github_counts_and_invisible_characters_are_shown(self):
        fake = self.github(self.H1, [])
        fake.blobs[(self.H1, "src/a.py")] = "one\ntwo\x0cstill two\nthree ‮\n".encode()
        code, out, err = self.show("--commit", self.H1, "src/a.py")
        self.assertEqual(code, 0, err)
        self.assertIn("(3 lines)", out)
        self.assertIn("     2\ttwo\\x0cstill two", out)
        self.assertIn("     3\tthree \\u202e", out)
        out = self.show("--commit", self.H1, "src/a.py", "--lines", "2:2")[1]
        self.assertNotIn("one", out)
        self.assertIn("     2\t", out)

    def test_the_file_is_read_at_the_given_commit_not_any_other(self):
        fake = self.github(self.H1, [])
        fake.blobs[(self.H1, "a.py")] = b"at head\n"
        fake.blobs[(self.H2, "a.py")] = b"elsewhere\n"
        self.assertIn("at head", self.show("--commit", self.H1, "a.py")[1])

    def test_a_large_file_needs_a_range_and_is_read_with_the_raw_media_type(self):
        fake = self.github(self.H1, [])
        fake.blobs[(self.H1, "big.txt")] = b"".join(b"line %d\n" % n for n in range(1, 200_000))
        code, _, err = self.show("--commit", self.H1, "big.txt")
        self.assertEqual(code, 2)
        self.assertIn("--lines", err)
        code, out, err = self.show("--commit", self.H1, "big.txt", "--lines", "150000:150001")
        self.assertEqual(code, 0, err)
        self.assertIn("150000\tline 150000", out)
        self.assertTrue(any(any("raw" in a for a in c["args"]) for c in fake.calls))

    def test_missing_paths_directories_and_binaries(self):
        fake = self.github(self.H1, [])
        fake.dirs[(self.H1, "src")] = [{"type": "file", "name": "a.py", "size": 3},
                                       {"type": "dir", "name": "lib"}]
        fake.blobs[(self.H1, "logo.png")] = b"\x89PNG\x00\x00"
        self.assertEqual(self.show("--commit", self.H1, "nope.py")[0], 2)
        out = self.show("--commit", self.H1, "src")[1]
        self.assertLess(out.index("lib/"), out.index("a.py"))
        self.assertIn("binary", self.show("--commit", self.H1, "logo.png")[1])
        self.assertEqual(self.show("--commit", "abc", "a.py")[0], 2)


class TestGitHubTransport(GitHubCase):
    def test_file_pages_are_requested_at_100_with_the_longer_timeout(self):
        fake = self.github(self.H1, [text_file("a.py", "@@ -1 +1 @@\n-x\n+y")])
        self.assertEqual(self.main("extract", "-R", "owner/repo", "-n", "1", "--commit", self.H1)[0], 0)
        call = next(c for c in fake.calls if "--paginate" in c["args"])
        self.assertIn("per_page=100", call["args"][1])
        self.assertEqual(call["timeout"], ghreview.GH_PAGINATED_TIMEOUT)

    def test_an_object_holding_a_line_separator_is_not_split_in_two(self):
        self.github(self.H1, [text_file("a.js", "@@ -1 +1 @@\n-s = ''\n+s = ' \u0085'")])
        code, out, err = self.main("map", "-R", "owner/repo", "-n", "1", "--commit", self.H1)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["files"][0]["right_ranges"], [[1, 1]])

    def test_lenses_reuse_the_file_list_but_stage_always_refetches(self):
        fake = self.github(self.H1, [text_file("a.py", "@@ -1 +1 @@\n-x\n+y")])
        for _ in range(3):
            self.main("extract", "-R", "owner/repo", "-n", "1", "--commit", self.H1, "a.py")

        def listings():
            return sum(1 for c in fake.calls if c["args"][1].endswith("/files?per_page=100"))
        self.assertEqual(listings(), 1)
        self.assertEqual(self.stage("owner/repo", {"verdict": "ready-to-merge", "reviewed": ["a.py"]}, self.H1)[0], 0)
        self.assertEqual(listings(), 2)

    def test_a_transient_failure_of_a_read_is_retried_once(self):
        fake = self.github(self.H1, [text_file("a.py", "@@ -1 +1 @@\n-x\n+y")])
        bad_gateway = (lambda args: "--paginate" in args, "gh: Bad Gateway (HTTP 502)\n")
        fake.failures.append(bad_gateway)
        self.assertEqual(self.main("extract", "-R", "owner/repo", "-n", "1", "--commit", self.H1)[0], 0)
        fake.failures.extend([bad_gateway, bad_gateway])
        with self.assertRaises(subprocess.CalledProcessError):
            ghreview.fetch_files("Owner/Repo", 1)

    def test_a_mutation_and_a_permanent_error_are_not_retried(self):
        fake = self.github(self.H1, [])
        fake.failures.append((lambda args: "POST" in args, "gh: Server Error (HTTP 500)\n"))
        with self.assertRaises(subprocess.CalledProcessError):
            ghreview.post_review("Owner/Repo", 1, self.H1, [], "body")
        self.assertEqual(sum(1 for c in fake.calls if "POST" in c["args"]), 1)
        before = len(fake.calls)
        with self.assertRaises(ValueError):
            ghreview.contents("Owner/Repo", self.H1, "missing.py")
        self.assertEqual(len(fake.calls) - before, 1)


class TestReceiptLifetime(GitHubCase):
    def test_a_receipt_that_proves_ownership_survives_the_age_sweep(self):
        fake = self.github(self.H1, [])
        fake.post_review({"commit_id": self.H1, "body": "",
                          "comments": [{"path": "a.py", "line": 2, "body": ghreview._mark("x")}]})
        rid = max(fake.reviews)
        ghreview.remember_review("Owner/Repo", 1, fake.reviews[rid], fake.comments[rid])
        receipt = ghreview.receipt_path("Owner/Repo", 1, rid)
        old = time.time() - 40 * 86400
        os.utime(str(receipt), (old, old))
        _, refusal = ghreview.pending_snapshot("Owner/Repo", 1)
        self.assertIsNone(refusal)
        ghreview.prune_receipts()
        self.assertTrue(receipt.exists())


class TestRepliesInOwnedDrafts(GitHubCase):
    """The procedure stages a review and then replies, so every owned draft that
    answered a thread holds the tool's own reply. Refusing those as
    unrecoverable parked the PR on every push."""

    FILES = [text_file("a.py", "@@ -1,1 +1,3 @@\n ctx\n+one\n+two")]

    def draft_with_reply(self):
        fake = self.github(self.H1, self.FILES)
        fake.threads = [{"id": "PRRT_1", "path": "a.py", "line": 2, "root_id": 555, "author": "me",
                         "body": "earlier finding"}]
        code, _, err = self.stage("Owner/Repo", {"verdict": "neutral", "reviewed": ["a.py"],
                                                 "comments": [{"path": "a.py", "line": 2, "body": "first"}]}, self.H1)
        self.assertEqual(code, 0, err)
        body = Path(self.tmp.name) / "reply.txt"
        body.write_text("Still open: the retry is missing.")
        code, _, err = self.main("reply", "-R", "Owner/Repo", "-n", "1", "--thread-id", "PRRT_1",
                                 "--commit", self.H1, "--body-file", str(body))
        self.assertEqual(code, 0, err)
        return fake

    def drafted(self, fake):
        return [(c["in_reply_to_id"], c["body"].split("\n")[0]) for cs in fake.comments.values() for c in cs]

    def test_an_owned_draft_holding_our_reply_is_replaced(self):
        fake = self.draft_with_reply()
        code, _, err = self.stage("Owner/Repo", {"verdict": "neutral", "reviewed": ["a.py"],
                                                 "comments": [{"path": "a.py", "line": 3, "body": "second"}]},
                                  self.H1, "--replace-pending")
        self.assertEqual(code, 0, err)
        # The old reply goes with the old draft; the reviewer re-adds replies after stage.
        self.assertEqual(self.drafted(fake), [(None, "second")])

    def test_a_failed_replacement_restores_the_roots_and_the_replies(self):
        fake = self.draft_with_reply()
        fake.failures.append((lambda args: "POST" in args, "gh: Validation Failed (HTTP 422)\n"))
        code, _, err = self.stage("Owner/Repo", {"verdict": "neutral", "reviewed": ["a.py"],
                                                 "comments": [{"path": "a.py", "line": 3, "body": "second"}]},
                                  self.H1, "--replace-pending")
        self.assertEqual(code, 1)
        self.assertEqual(sorted(self.drafted(fake), key=str),
                         sorted([(None, "first"), (555, "Still open: the retry is missing.")], key=str))
        # The restored draft is recognised as ours again, replies included.
        snapshot, refusal = ghreview.pending_snapshot("Owner/Repo", 1)
        self.assertIsNone(refusal)
        self.assertFalse(snapshot["forced"])

    def test_a_reply_with_no_thread_to_return_to_is_still_preserved(self):
        fake = self.draft_with_reply()
        fake.threads = []
        before = self.drafted(fake)
        data = Path(self.tmp.name) / "in2.json"
        data.write_text(json.dumps({"verdict": "neutral", "reviewed": ["a.py"],
                                    "comments": [{"path": "a.py", "line": 3, "body": "second"}]}))
        code, out, _ = self.main("stage", "-R", "Owner/Repo", "-n", "1", "--commit", self.H1,
                                 "--input", str(data), "--replace-pending")
        self.assertEqual(code, 3)
        self.assertIn("reply", json.loads(out)["reason"])
        self.assertEqual(self.drafted(fake), before)


class TestStaleListing(GitHubCase):
    OLD = "0" * 40
    FILES = [text_file("a.py", "@@ -1 +1 @@\n-x\n+y"), text_file("gone.py", "@@ -1 +0,0 @@\n-z", status="removed")]

    def listings(self, fake):
        return sum(1 for c in fake.calls if "/files?" in c["args"][1])

    def test_a_listing_of_the_previous_head_is_fetched_again(self):
        fake = self.github(self.H1, self.FILES)
        fake.listing_refs = [self.OLD]
        code, out, err = self.main("extract", "-R", "owner/repo", "-n", "1", "--commit", self.H1, "a.py")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.listings(fake), 2)

    def test_a_listing_that_stays_stale_is_refused(self):
        fake = self.github(self.H1, self.FILES)
        fake.listing_refs = [self.OLD, self.OLD]
        code, _, err = self.stage("owner/repo", {"verdict": "ready-to-merge", "reviewed": ["a.py", "gone.py"]}, self.H1)
        self.assertEqual(code, 2)
        self.assertIn("another head", err)
        self.assertIsNone(ghreview.load_verdict("Owner/Repo", 1))

    def test_a_removed_file_naming_the_base_is_not_stale(self):
        fake = self.github(self.H1, self.FILES)
        self.assertEqual(self.main("map", "-R", "owner/repo", "-n", "1", "--commit", self.H1)[0], 0)
        self.assertEqual(self.listings(fake), 1)


class TestVerdictRecordForWatcher(unittest.TestCase):
    def test_the_record_carries_per_file_coverage_and_the_carry_source(self):
        report = {"patches": {"a.py": "x"}, "coverage": {"carried_from": "1" * 40}}
        record = ghreview.verdict_record("neutral", "2" * 40, "d", report)
        self.assertEqual((record["patches"], record["carried_from"]), ({"a.py": "x"}, "1" * 40))


if __name__ == "__main__":
    unittest.main()
