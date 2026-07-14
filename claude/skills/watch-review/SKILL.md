---
name: watch-review
description: >
  One polling tick of the review watcher: check the current repo for open,
  non-draft PRs where Leo's GitHub user is DIRECTLY requested as reviewer,
  run /review-pr on each new one, and record it in machine-local state so it
  is never auto-reviewed again. Meant to run repeatedly via
  "/loop 1m /watch-review".
when_to_use: >
  ONLY when Leo explicitly invokes /watch-review (usually inside /loop).
  Never trigger it because a PR or review was merely mentioned — reviewing a
  specific PR is /review-pr; nothing else warrants the watcher.
model: haiku
allowed-tools:
  - Bash(gh *)
  - Bash(python3 *)
  - Skill
---

# /watch-review — one tick of the review-request watcher

Scope: the current directory's repo only. A tick is cheap by design — on an
idle tick this haiku loop reads the preflight and says one line. Only a match
escalates (invoking /review-pr switches the turn to opus via that skill's own
frontmatter; this file must NOT set `disable-model-invocation` — skills marked
that way do not execute under /loop).

## Preflight (injected)

- Repo: !`gh repo view --json nameWithOwner 2>&1`
- Me: !`gh api user --jq .login 2>&1`
- Directly-requested open PRs: !`gh pr list --state open --search "user-review-requested:@me" --json number,title,isDraft,reviewRequests 2>&1`
- Watcher state (all repos): !`python3 ${LEOS_AGENT_PATH:-$HOME/.leos-agent}/claude/scripts/state.py get review-watcher 2>&1`

Not a repo, gh unauthenticated, or the PR listing errored → stop with a
one-line diagnosis; touch nothing.

## Filter

`user-review-requested:@me` already matches only PRs where I am **directly**
requested — a request for a team I belong to does not count and must never
trigger a review. Belt and braces, from the preflight list keep only PRs
where ALL hold:

1. `isDraft` is false — drafts are skipped, not recorded; the watcher picks
   them up on a later tick once marked ready.
2. `reviewRequests` contains an entry with `"__typename": "User"` and
   `"login"` equal to my login (drops team requests and stale search results).
3. The PR number is NOT in `reviewed` for this repo's `nameWithOwner` key in
   the watcher state.

Nothing left → reply exactly one line — `review-watcher: no new review
requests for <owner/repo>` — and end the turn. The loop re-fires next
interval.

## Review and record

For each remaining PR, in ascending number order, strictly sequentially:

1. Invoke the Skill tool: skill `review-pr`, args `<number>`.
2. **Only after /review-pr completes** (verdict delivered), record it:

   ```
   python3 ${LEOS_AGENT_PATH:-$HOME/.leos-agent}/claude/scripts/state.py \
     merge review-watcher "<owner/repo>" '{"reviewed": [<number>]}'
   ```

   Never skip or reorder this write: a staged (pending, unsubmitted) review
   does NOT clear the review request on GitHub, so this state file is the
   ONLY thing preventing the next tick from re-reviewing the same PR.
3. If /review-pr failed or aborted: do NOT record the number — the next tick
   retries it. Surface the error in this tick's report; if the same PR keeps
   failing, say so plainly each tick so Leo can intervene.

Then report one line per PR: `#<number> <title> — <verdict>, <n> comments
staged`, plus any failures.

## Rules

- **Once recorded, never auto-reviewed again** — not even after new commits
  to the PR. Leo re-reviews manually with `/review-pr <number>` when he wants
  a second pass.
- The watcher never submits reviews, never comments publicly, never touches
  PRs where I'm not directly requested. All review output is staged by
  /review-pr as pending.
- GitHub search silently returns zero for a mistyped qualifier — it looks
  identical to "no PRs waiting". If the watcher seems permanently idle while
  requests exist, sanity-check with `gh pr list --search "review-requested:@me"`
  (the team-inclusive variant) to confirm the plumbing.
