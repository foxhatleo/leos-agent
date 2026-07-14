---
name: review-pr
description: >
  Review a GitHub pull request of the current repo and stage inline review
  comments that remain PENDING on GitHub — visible only to Leo, never
  submitted. Handles Leo's existing reviews: a stale pending review is
  replaced; posted threads are left, resolved, or get a staged reply.
  Reports the staged comments and a merge verdict in chat. Requires gh,
  installed and authenticated.
when_to_use: >
  Leo asks to review a pull request by number ("review PR 42", "/review-pr 42")
  or "review the PR for this branch". NOT for reviewing the local working diff
  (that is /code-review or the reviewer subagent) and NOT for submitting a
  review — this only stages draft comments.
argument-hint: "[pr-number]"
model: opus[1m]
allowed-tools:
  - Bash(gh *)
  - Bash(git *)
  - Bash(python3 *)
  - Agent
---

# /review-pr — stage a pending GitHub review

Tier map: sonnet reads (lens agents), opus judges (this main loop). The staged
review is created by `${CLAUDE_SKILL_DIR}/scripts/ghreview.py` in ONE API call
with no `event` field — that is what keeps it PENDING. Never use `gh pr review`
(it always submits) and never set an `event` value.

## Preflight (injected)

- Auth: !`gh auth status 2>&1 | head -3`
- PR: !`gh pr view $0 --json number,title,body,author,baseRefName,headRefName,headRefOid,isDraft,additions,deletions,changedFiles,url,reviews 2>&1`
- CI: !`gh pr checks $0 2>&1 | head -15`

If the PR fetch above errored (not a repo, unauthenticated, no such PR, no PR
for the current branch), stop with a one-line diagnosis. Otherwise parse
`OWNER/REPO` **from the PR's `url` field** — not from `origin` — and pass it as
`-R OWNER/REPO` on every later `gh`/script call so fork setups work.

`$0` is the PR number (omit → current branch's PR, use the `number` field from
the preflight JSON). Any further arguments are focus hints (e.g. "focus on the
migration") — weight the review accordingly but still cover the whole diff.

## Step 1 — Existing reviews by me

Two kinds of prior review state, handled differently:

**A pending (staged) review of mine** — clear it and re-review from scratch
(Leo's standing rule), but the script only auto-deletes when every comment on
it carries the script's own marker (it embeds one in everything it stages):

```
python3 ${CLAUDE_SKILL_DIR}/scripts/ghreview.py clear-pending -R OWNER/REPO -n N
```

If it exits 0, note what was deleted in the final report. If it exits 3, it
refused — the pending review holds at least one comment this script didn't
stage (likely something Leo hand-drafted). Print the JSON report verbatim to
Leo and ask whether to discard it; only re-run with `--force` (or, at the
stage step, `--replace-pending --force`) once he confirms. Still pass
`--replace-pending` at the stage step as a race guard.

**Posted (submitted) review threads of mine** — fetch them:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/ghreview.py threads -R OWNER/REPO -n N
```

Returns unresolved threads whose root comment is mine (threads from pending
reviews are excluded automatically; `line` is null for file-level threads).
For each thread, judge the original comment against the **current** diff
(`ghreview.py extract` for that path — `is_outdated` means the nearby code
changed, which is a hint, not a verdict) and pick one action, defaulting to
*leave* when torn:

| Judgment | Action |
|---|---|
| Issue no longer applies (fixed, code removed, moot) | **Resolve** the thread — applied in Step 5. |
| Still applies, `replies_after_mine: false` | **Leave** untouched. |
| Still applies, `replies_after_mine: true` | **Reply**: draft a response in the Step 4 voice — answer their actual point, concede plainly when they're right (if they're right that it's moot, resolve instead of replying). Staged in Step 5, never posted directly. |

Hold the chosen actions until Step 5 — no mutations happen before
adjudication is complete.

## Step 2 — Map the diff and pick a route

```
python3 ${CLAUDE_SKILL_DIR}/scripts/ghreview.py map -R OWNER/REPO -n N
```

Returns per-file addressable-line ranges, `generated` flags (lockfiles, dist,
snapshots — excluded from review, noted in the report), and totals. Route on
the post-exclusion size:

| Size | Route |
|---|---|
| ≤ ~150 changed lines and ≤ 3 files | **Solo**: no fan-out; read `gh pr diff N` here and review directly. |
| Standard | **3 lens agents**, each over the full file set. |
| > ~40 files or > ~3000 lines | **Sharded**: partition files into groups of ~15 by directory; run the 3 lenses per shard; cap ~9 lens agents total. Beyond the cap, rank files by non-test source lines changed, review the top set, and disclose the unreviewed remainder — the verdict then caps at *neutral*. |

## Step 3 — Lens fan-out (sonnet, parallel)

Spawn three subagents in a single message (Agent tool, `model: sonnet`,
general-purpose). Do NOT ingest the full diff in this main loop on the standard
path — the lenses read, you judge. Each lens gets: PR number, `OWNER/REPO`,
title/body, its file list, and instructions to fetch its own diff slice via
`gh pr diff N` or `python3 <skill-dir>/scripts/ghreview.py extract -R OWNER/REPO -n N <paths…>`
(pass the absolute skill dir into the prompt).

Charters:
1. **Correctness** — logic errors, off-by-ones, broken control flow, behavior
   that contradicts the PR's stated intent.
2. **Safety** — unhandled error paths, concurrency/races, resource leaks,
   injection/authz, data loss, unvalidated input.
3. **Design & tests** — API contract regressions, missing tests for changed
   behavior, dead code, misleading names, genuine style nits worth a human's
   comment.

Each lens returns findings as JSON only:
`[{path, line, side: "RIGHT"|"LEFT", severity: "blocking"|"major"|"minor"|"nit", confidence: 0-100, note, fix?}]`
with `line` as the absolute new-file line (RIGHT) it verified against the
patch, and an instruction to cite the exact diff line — unverifiable findings
get dropped in Step 4, so guessing wastes the lens's own work.

## Step 4 — Adjudication (this loop, opus)

For every candidate finding: pull the implicated file's patch
(`ghreview.py extract`), confirm the finding is real against the actual diff,
drop what you cannot confirm or what a competent human reviewer wouldn't
bother writing, dedupe across lenses, then rewrite survivors in the voice
below. Cap at **15 comments**, priority blocking > major > minor > nit.

Also dedupe against Step 1's still-open threads: a finding that repeats an
existing thread of mine (same file, overlapping lines, same issue) is never
staged as a new comment — the thread's leave/reply action already covers it.

### Voice — every comment must pass these rules

- One or two sentences. Lead with the problem. No greeting, praise, sign-off,
  emoji, or hedging stacks ("it seems like it might potentially…").
- Never restate what the code does — the author knows. Say what breaks or is
  wrong; when the fix is non-obvious, add it in a clause.
- Genuine questions are fine ("is the empty-list case reachable here?") —
  never as passive-aggressive wrappers for assertions.
- Prefix minor/style items with `nit:`.
- GitHub ```suggestion``` blocks only for mechanical fixes of ≤3 lines.
- Ban list (any occurrence → rewrite): "Great", "Nice", "Awesome",
  "I noticed that", "It's worth noting", "As an AI", "Consider" as a sentence
  opener, "This is a minor point, but", any emoji.

| Bad | Good |
|---|---|
| "Great work! However, I noticed there might be a potential issue where the error could possibly be ignored." | "`err` from `parse()` is dropped — a malformed config silently falls through to defaults." |
| "Consider adding a null check to improve robustness. 🙂" | "`user` is nil when the session expired mid-request; this panics. Guard before the deref." |
| "It's worth noting this loop could be optimized." | "nit: this is O(n²) via `includes`; a Set lookup keeps it linear. Fine if n stays small." |

## Step 5 — Apply: stage comments, stage replies, resolve threads

Strictly in this order (comments and replies are invisible-until-submit;
resolutions are public and go last, only once staging has succeeded):

1. **Stage new comments.** Write them to a JSON file in the scratchpad
   (`{"comments": [{path, line, side, body, start_line?, start_side?}]}`), then:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/ghreview.py stage -R OWNER/REPO -n N \
     --commit <headRefOid> --input comments.json --replace-pending
   ```

   The script re-validates every line against the hunk map (snaps within a
   hunk, drops what can't anchor — one bad line would 422 the entire review),
   POSTs once with no `event`, and retries once against a refreshed head on
   422. Use `--dry-run` first if any line anchors feel uncertain. Zero new
   comments → skip this sub-step; **never create an empty review just for
   comments** (the reply sub-step creates its own shell when needed). Every
   staged comment is auto-marked with the script's hidden marker, which is
   what lets a later clear-pending tell "staged by this skill" apart from
   anything hand-drafted. With `--replace-pending`, the same guarded delete as
   Step 1 applies — a mixed pending review makes `stage` exit 3 (refused)
   *before* posting anything new; surface the report and get Leo's go-ahead
   before retrying with `--force`.

2. **Stage thread replies** — one call per Step 1 reply action, body from a
   scratchpad file:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/ghreview.py reply -R OWNER/REPO -n N \
     --thread-id PRRT_… --body-file reply.txt
   ```

   Attaches to the pending review from sub-step 1, or creates an empty
   pending shell first when there were no new comments. Replies stay pending
   alongside everything else.

3. **Resolve stale threads** — one call per Step 1 resolve action:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/ghreview.py resolve-thread -R OWNER/REPO -n N \
     --thread-id PRRT_…
   ```

   This is the one immediate, publicly visible action in the whole skill
   (GitHub has no staged resolution) — say so in the report. A denial
   (resolving needs PR authorship or write access) is not a failure: leave
   the thread and note it.

If sub-step 1 failed hard (422 after retry), apply nothing else: report all
findings, replies, and would-be resolutions chat-only with the verbatim API
error.

## Step 6 — Report (chat only)

1. Staged comments as a table: `path:line — comment`.
2. Existing threads as a table: `path:line — left / resolved / reply staged`
   (+ what was said in staged replies; note if a stale pending review was
   replaced, and that resolutions are already live).
3. Unstaged findings (dropped anchors, overflow past the cap) — clearly marked.
4. Coverage: excluded generated files, unreviewed files on huge PRs, CI status.
5. **Verdict** with 1–2 lines of rationale, from this rubric:
   - **ready-to-merge** — no blocking or major findings; CI green or clearly
     unrelated; full coverage.
   - **neutral** — real but non-blocking findings, missing tests for changed
     behavior, partial coverage, or CI red/unknown. Default when torn.
   - **seriously-problematic** — at least one *verified* blocking finding:
     broken main-path behavior, data loss/corruption, a vulnerability, an
     unacknowledged breaking API change, or the diff doesn't do what the PR
     claims. This maps to "would warrant request-changes" — say so, but never
     submit any review event.
6. Close with: "Comments are staged as a pending review — only you can see
   them until you submit or discard on GitHub."

## Edge cases

| Situation | Behavior |
|---|---|
| My pending review exists | Deleted automatically in Step 1 and re-reviewed from scratch only if every comment on it is marker-tagged; otherwise the script refuses (exit 3) — surface the report and ask Leo before `--force`. |
| Someone replied in my thread | Reply drafted and staged into the pending review — never posted directly. |
| My comment no longer applies | Thread resolved (immediate — GitHub can't stage this); disclosed in the report. |
| Resolve denied (no write access, not PR author) | Thread left as-is; noted in the report. |
| Unsure whether a thread still applies | Leave it — resolving someone into silence is worse than a stale thread. |
| Zero findings | No review created (unless replies need a pending shell); verdict still reported. |
| Huge PR | Shard; cap agents; disclose coverage; verdict ≤ neutral if partial. |
| Fork PR | `OWNER/REPO` from PR url; never checkout; review is API-only. |
| Own PR | Pending reviews on your own PR work; no special case. |
| New push mid-review | Stage script re-anchors against the refreshed head automatically. |
| 422 after retry | Report findings chat-only with the verbatim API error; don't loop. |
