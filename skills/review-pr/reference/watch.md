# Marking a PR watched: leos-agent

Every `/review-pr` pass marks its PR watched, so the review watcher follows it
until it merges or closes: new heads are re-reviewed and replies addressed to
the user come back as follow-ups. A draft pauses it. There is no unwatch.

Pin the head, then mark it:

```
gh pr view N -R OWNER/REPO --json headRefOid -q .headRefOid
python3 "<plugin-root>/scripts/watch_review.py" watch -R OWNER/REPO N --head SHA
```

It prints `{"repo","number","watched","round","round_kind"}`. With `--head`,
the watcher does not emit that head while this pass has it, and the pass
counts as a round. A retry at the same head and kind keeps its round.

Pass `round` and `round_kind` to the reviewer in its brief, and use them in
the PR's header (`reference/submit.md`). `round_kind` is `initial`,
`commit`, `comment`, `commit+comment` or `merge-only`.

Inside a watch, a line's `start` already printed `round` and `round_kind`;
do not call `watch` for that claim.

Marking needs no running monitor; `/watch-review` in this repository picks the
PR up. A refusal (GitHub does not know the repository, gh not signed in) does
not stop the review: say so once and go on.
