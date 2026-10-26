# Presenting and submitting a review — leos-agent

Read this after the review is staged, from the reviewer's report or your own
local pass. Nothing goes public here without the user's answer, except in a
watch the user started as `fully-auto`.

## Show it

One block per PR, its `###` heading the divider so PRs never run together.
In order: heading, who and which round, context, then the question.

```
### owner/repo#N — <title>
@alice · round 2 · re-review (3 new commits)
**Verdict:** neutral → COMMENT · CI: failing (informational)
**Found:** `api/page.ts:48` major: the last page is dropped when total % size == 0.
**Others:** Bugbot's null deref at `api/page.ts:12` can't happen (guarded at :9).
**Plan:** submit COMMENT with 1 comment; resolve your thread on :30 (fixed).
```

The author is the watch line's `by=`; the round is what `start` printed
(outside a watch: your submitted reviews on the PR plus one). Use a table for
several findings. Write like a person: one to three short sentences per idea,
casual, plain words, no em dashes or run-ons.

The event follows the verdict: ready-to-merge APPROVE, neutral COMMENT,
seriously-problematic REQUEST_CHANGES. If the user's approval still stands
(`gh pr view N -R OWNER/REPO --json latestReviews`) on a ready-to-merge
verdict, submit nothing new, or COMMENT for new minor comments; if GitHub
dismissed it, plan APPROVE again.

## Ask

Show every ready block, then ask once: one question per PR, its header chip
`#N` (AskUserQuestion where the harness has it, up to four per call;
otherwise ask in plain text and wait): "Submit as <EVENT>
(Recommended)", "Submit as COMMENT" when the event is not COMMENT, and "Leave
pending". Anything else the user writes is an edit: restage or rewrite, show
it again, and ask again. In a `fully-auto` watch, skip the question and carry
out the plan.

## Do it

The answer can come long after the plan, so act only on what is still true.
`submit` refuses a moved head, and passing each thread's `latest_comment_id`
(from planning) as `--expect-latest` makes `reply` and `resolve-thread` refuse
a thread someone added to since. On such a refusal, re-read, show what
changed, and ask again; never push the old plan through.

1. `python3 "<plugin-root>/scripts/ghreview.py" submit -R OWNER/REPO -n N --commit SHA --event EVENT --body-file summary.txt`.
   The summary is one or two sentences, required for COMMENT and
   REQUEST_CHANGES and optional for APPROVE. An event the verdict does not map
   to, other than COMMENT, needs `--user-decision "<the user's words>"`. Exit 3
   means the user edited the draft: it is theirs to submit on GitHub.
2. Each proposed resolution:
   `python3 "<plugin-root>/scripts/ghreview.py" resolve-thread -R OWNER/REPO -n N --thread-id PRRT_… --commit SHA --evidence-file FILE --expect-latest ID`.

"Leave pending" changes nothing; the draft waits for the user. Report what
was posted, with the review URL, and never call a pending one submitted.
