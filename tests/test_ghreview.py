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
        _, refusal = self.snapshot(with_reply)
        self.assertIn("replies", refusal["reason"])
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


if __name__ == "__main__":
    unittest.main()
