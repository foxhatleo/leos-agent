"""The review watcher's two decisions: what is eligible, and what to emit.

Both are pure functions over a `gh pr list` payload and the state file, so they
are tested without a network or a clock. The gates matter for cost, not just
correctness: a review that should not have fired is a reviewer subagent plus its
lens fan-out, each paying a cold cache write.
"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_watcher():
    spec = importlib.util.spec_from_file_location("watch_review_test", ROOT / "scripts" / "watch_review.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pr(number=1, head="a" * 40, draft=False, requested=("leo",), reviews=()):
    return {
        "number": number,
        "title": "Fix the retry backoff",
        "url": f"https://github.com/o/r/pull/{number}",
        "isDraft": draft,
        "headRefOid": head,
        "reviewRequests": [{"__typename": "User", "login": name} for name in requested],
        "latestReviews": [{"author": {"login": who}, "state": state} for who, state in reviews],
    }


class TestEligibility(unittest.TestCase):
    def setUp(self):
        self.watcher = load_watcher()

    def numbers(self, listing):
        return [p["number"] for p in self.watcher.eligible(listing, "leo")]

    def test_direct_request_is_eligible(self):
        self.assertEqual(self.numbers([pr(1)]), [1])

    def test_drafts_and_team_only_requests_are_dropped(self):
        self.assertEqual(self.numbers([pr(1, draft=True)]), [])
        team = pr(2)
        team["reviewRequests"] = [{"__typename": "Team", "login": "leo"}]
        self.assertEqual(self.numbers([team]), [])

    def test_approval_by_someone_else_disqualifies(self):
        # Reviewing a stamped pull request changes nothing and costs a full
        # reviewer fan-out, so this one never reaches a model at all.
        self.assertEqual(self.numbers([pr(1, reviews=[("dana", "APPROVED")])]), [])

    def test_own_approval_and_other_states_do_not_disqualify(self):
        self.assertEqual(self.numbers([pr(1, reviews=[("leo", "APPROVED")])]), [1])
        self.assertEqual(self.numbers([pr(2, reviews=[("dana", "COMMENTED")])]), [2])
        self.assertEqual(self.numbers([pr(3, reviews=[("dana", "CHANGES_REQUESTED")])]), [3])

    def test_one_approval_among_several_reviews_still_disqualifies(self):
        listing = [pr(1, reviews=[("dana", "COMMENTED"), ("sam", "APPROVED")])]
        self.assertEqual(self.numbers(listing), [])

    def test_results_are_ordered_by_number(self):
        self.assertEqual(self.numbers([pr(9), pr(2), pr(5)]), [2, 5, 9])


class TestEmitDecision(unittest.TestCase):
    def setUp(self):
        self.watcher = load_watcher()

    def due(self, matches, known, first_seen=None, emitted=None, now=1000.0, settle=0):
        return self.watcher.due(matches, known, first_seen if first_seen is not None else {},
                                emitted if emitted is not None else set(), now, settle)

    def test_unseen_pull_request_is_a_new_review(self):
        [(verb, item, previous)] = self.due([pr(1, head="abc")], {})
        self.assertEqual((verb, item["number"], previous), ("review-requested", 1, ""))

    def test_unmoved_head_is_silent(self):
        self.assertEqual(self.due([pr(1, head="abc")], {1: "abc"}), [])

    def test_moved_head_is_a_re_review_naming_the_old_one(self):
        [(verb, _, previous)] = self.due([pr(1, head="def")], {1: "abc"})
        self.assertEqual((verb, previous), ("re-review", "abc"))

    def test_each_head_is_emitted_once_per_process(self):
        emitted = {(1, "abc")}
        self.assertEqual(self.due([pr(1, head="abc")], {}, emitted=emitted), [])
        # ...but a push within the same process comes back
        self.assertEqual(len(self.due([pr(1, head="def")], {}, emitted=emitted)), 1)

    def test_settle_window_suppresses_then_releases(self):
        first_seen, emitted = {}, set()
        self.assertEqual(self.due([pr(1, head="abc")], {}, first_seen, emitted, now=1000.0, settle=120), [])
        self.assertEqual(self.due([pr(1, head="abc")], {}, first_seen, emitted, now=1060.0, settle=120), [])
        self.assertEqual(len(self.due([pr(1, head="abc")], {}, first_seen, emitted, now=1121.0, settle=120)), 1)

    def test_a_push_during_the_settle_window_restarts_it(self):
        first_seen, emitted = {}, set()
        self.due([pr(1, head="abc")], {}, first_seen, emitted, now=1000.0, settle=120)
        # a new head is a new key, so it waits its own full window
        self.assertEqual(self.due([pr(1, head="def")], {}, first_seen, emitted, now=1100.0, settle=120), [])
        self.assertEqual(len(self.due([pr(1, head="def")], {}, first_seen, emitted, now=1221.0, settle=120)), 1)


class TestStateMigration(unittest.TestCase):
    def setUp(self):
        self.watcher = load_watcher()

    def test_legacy_number_list_migrates_to_an_unknown_head(self):
        heads = self.watcher.heads_of({"reviewed": [27532, 27540]})
        self.assertEqual(heads, {27532: "", 27540: ""})

    def test_a_migrated_entry_comes_back_once_then_tracks(self):
        known = self.watcher.heads_of({"reviewed": [1]})
        [(verb, _, previous)] = self.watcher.due([pr(1, head="abc")], known, {}, set(), 1000.0, 0)
        # It is a re-review -- the number is known -- but there is no old head to name.
        self.assertEqual((verb, previous), ("re-review", ""))
        self.assertEqual(self.watcher.due([pr(1, head="abc")], {1: "abc"}, {}, set(), 1000.0, 0), [])

    def test_heads_win_over_a_stale_legacy_entry(self):
        heads = self.watcher.heads_of({"reviewed": [1], "heads": {"1": "abc"}})
        self.assertEqual(heads, {1: "abc"})


if __name__ == "__main__":
    unittest.main()
