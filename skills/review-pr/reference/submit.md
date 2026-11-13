# Presenting and submitting a review: leos-agent

Read this once a review is staged. Reviewers only stage and propose; you
show the plan, ask, and publish. Nothing goes public without the user's
answer for that PR.

## Show it

One block per PR:

```
## Alice Chen [ABC-123] Fix pagination for empty pages
Round #2: New commit
owner/repo#N at 1a2b3c4; two commits since round 1 fix the page size and add tests.

| Where | Severity | Comment |
|---|---|---|
| `api/page.ts:48` | major | The last page is dropped when total % size == 0. |

**Others:** Copilot's null deref at `api/page.ts:12` can't happen (guarded at :9).
**CI:** e2e failed, flaky and unrelated.
**Plan:** submit COMMENT "One edge case left on the last page."; resolve your thread on :30 (fixed).
```

- Heading: the PR author's display name, else login (`gh pr view N -R OWNER/REPO --json author`),
  then the report's ticket key in brackets, else `owner/repo#N`, then the PR title.
- Round: `round` from `start` or `watch` (`reference/watch.md`), and the label for
  `round_kind`: initial `Initial review`, commit `New commit`, comment `Comment response`,
  commit+comment `New commit + Comment response`, merge-only `New commit (base merge only)`.
- Then context (`owner/repo#N at <short SHA>`, what changed since the last round), findings, what others
  said (bots, tools, people; verified or not), CI notes, `Exercise:` if any,
  and the plan.

## Ask

Before asking, snapshot what the plan rests on (`$SCRATCH` is an absolute
temporary directory outside the repository):

```
python3 "<plugin-root>/scripts/ghreview.py" snapshot -R OWNER/REPO -n N --commit SHA --out "$SCRATCH/snap-N.json"
```

In a watch, also `watch_review.py await` the claim (watch-review's skill).

Ask about one PR per call: a blocking AskUserQuestion, header `#N` (12
characters at most); without the tool, ask in plain text and wait. Options:
"Do the plan (Recommended)", one or two real alternatives (such as "Submit as
COMMENT" or "Approve without the nits"), "Run checks first" only when the
report has `Exercise:` and the PR is not a fork, and "Leave pending". Anything
else the user writes is an edit: restage or rewrite, show it again, ask again.

## Do it

The answer can come long after the question: proceed only where safe.

1. `python3 "<plugin-root>/scripts/ghreview.py" recheck -R OWNER/REPO -n N --snapshot "$SCRATCH/snap-N.json"`.
2. Apply the answer to what `valid` still allows, in `reference/followup.md`'s order:
   - `valid.submit`: `python3 "<plugin-root>/scripts/ghreview.py" submit -R OWNER/REPO -n N --commit SHA --event EVENT --body-file "$SCRATCH/body.txt" --expect-draft FP`,
     with FP the snapshot's `draft.fingerprint`, or `none` without a draft. Exit 3
     means the user edited the draft or it changed: it is theirs to submit.
   - each thread in `valid.replies`: `reply ... --post --expect-latest ID`, ID
     being that thread's entry in the snapshot's `threads`.
   - each thread in `valid.resolves`: `python3 "<plugin-root>/scripts/ghreview.py" resolve-thread -R OWNER/REPO -n N --thread-id PRRT_… --commit SHA --evidence-file FILE --expect-latest ID`.
   - `valid.comments`: `comment ... --body-file FILE --expect-latest ID|none [--in-reply-to ID]`,
     ID being the snapshot's `latest_conversation`.
3. What drifted (a moved head, a changed draft, a newer comment): dispatch a
   reviewer for just that, then ask again.
4. A merge or close, or the user's own GitHub actions (`my_new_reviews`): one
   line, "Since you were asked: ...", and skip what it overtook.
5. Then the next PR.

"Run checks first": run each proposed command through
`review_worktree.py run ... --approved "<their words>" -- CMD...` as
`reference/execution.md` says, hand `exit`, `tail` and `log` to a reviewer to
judge, then show the updated block and ask again.

"Leave pending" runs only `ghreview.py leave-pending -R OWNER/REPO -n N`, so
a later pass asks before replacing the draft. Report what was posted, with the
review URL, and never call a pending review submitted.

## Approvals

The event follows the verdict: ready-to-merge APPROVE, neutral COMMENT,
seriously-problematic REQUEST_CHANGES. An event the verdict does not map to,
other than COMMENT, needs `--user-decision "<the user's words>"`.

- APPROVE always carries a short, specific one-line body, never "LGTM".
  Minor findings and concrete test gaps may ride on it as minor or `nit:`.
- The user's approval still stands (`gh pr view N -R OWNER/REPO --json latestReviews`)
  or was dismissed, and a new head with real changes reviewed clean:
  re-approve with one line.
- A `re-approve` event: no re-review. Offer a ready line such as
  "Re-approving after the base merge, diff unchanged." and submit it on a yes.
