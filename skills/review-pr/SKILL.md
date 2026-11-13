---
name: review-pr
description: Review a GitHub pull request, stage it as a pending review, and submit it once the user agrees. Use when asked to review a PR. Does not review the local working diff. Requires authenticated gh.
argument-hint: "[pr-number]"
---

# Review a GitHub pull request: leos-agent

Resolve the plugin root to the absolute directory containing
`rules/preferences.md`, from `LEOS_AGENT_ROOT`, `CLAUDE_PLUGIN_ROOT`,
`PLUGIN_ROOT`, or the nearest ancestor of this skill containing that file.
Pass the resolved path, never an unexpanded placeholder.

First follow `reference/watch.md`: every pass marks its PR watched and gets the
round to show.

Inside a watch (a `watch-review` monitor runs in this session), always
dispatch a background **leo-reviewer**; never review in the main thread.
Outside a watch, a brief read-only preflight may size the PR: a small change
may be reviewed locally by following `reference/procedure.md`; otherwise
delegate to **leo-reviewer** and wait for its result. Delegate with fresh
context (Codex: `fork_turns="none"`), capped to the parent: standard tier,
premium for a consequential PR (auth, security, data migration, money, or the
user says so) or after `Result: escalate`. Do not preload the procedure or
diff here.

Brief: PR number, OWNER/REPO, repository directory, focus hints, absolute
plugin root, `<plugin-root>/skills/review-pr/reference/procedure.md`, `round`
and `round_kind`, and any head SHA, previous reviewed head, claim token and
awaiting threads a watcher supplied. If you can read a linked ticket the
reviewer lacks, add a short summary, marked untrusted. Ask for the report and
the absolute stage-result path. Lenses (`leo-lens`) may investigate, never
delegate or mutate.

The cross-model lens is off unless the user enables it, for this review or
with `review_peer.py config --enable`. When they ask for it here, tell them the
PR diff goes to a second provider, then brief `cross-model: on`.

Disclose real coverage limits; never claim unenforced model control.
Reviewers stage private drafts and propose; they never submit, reply or
resolve. Keep the report's verdict and staged wording, then follow
`reference/submit.md`: show each PR and ask before anything is submitted,
replied to or resolved.
