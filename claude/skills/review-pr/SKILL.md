---
name: review-pr
description: >
  Review a GitHub pull request of the current repo and stage inline review
  comments that remain PENDING on GitHub — visible only to Leo, never
  submitted. Reports the staged comments and a merge verdict in chat.
  Requires gh, installed and authenticated.
when_to_use: >
  Leo asks to review a pull request by number ("review PR 42", "/review-pr 42")
  or "review the PR for this branch". NOT for reviewing the local working diff
  (that is /code-review or the reviewer subagent) and NOT for submitting a
  review — this only stages draft comments.
argument-hint: "[pr-number]"
model: opus
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

## Step 1 — Pending-review conflict

The preflight `reviews` array may include a review with `state: "PENDING"`. If
one exists for the authenticated user (confirm with
`python3 ${CLAUDE_SKILL_DIR}/scripts/ghreview.py pending -R OWNER/REPO -n N`),
ask Leo via AskUserQuestion: **Replace it** (pass `--replace-pending` at the
stage step) or **Abort**. Never delete a pending review silently — it may hold
his half-written comments.

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

## Step 5 — Stage

Write the final comments to a JSON file in the scratchpad
(`{"comments": [{path, line, side, body, start_line?, start_side?}]}`), then:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/ghreview.py stage -R OWNER/REPO -n N \
  --commit <headRefOid> --input comments.json [--replace-pending]
```

The script re-validates every line against the hunk map (snaps within a hunk,
drops what can't anchor — one bad line would 422 the entire review), POSTs
once with no `event`, and retries once against a refreshed head on 422. Use
`--dry-run` first if any line anchors feel uncertain. **Zero findings after
adjudication → skip this step entirely**; never create an empty review.

## Step 6 — Report (chat only)

1. Staged comments as a table: `path:line — comment`.
2. Unstaged findings (dropped anchors, overflow past the cap) — clearly marked.
3. Coverage: excluded generated files, unreviewed files on huge PRs, CI status.
4. **Verdict** with 1–2 lines of rationale, from this rubric:
   - **ready-to-merge** — no blocking or major findings; CI green or clearly
     unrelated; full coverage.
   - **neutral** — real but non-blocking findings, missing tests for changed
     behavior, partial coverage, or CI red/unknown. Default when torn.
   - **seriously-problematic** — at least one *verified* blocking finding:
     broken main-path behavior, data loss/corruption, a vulnerability, an
     unacknowledged breaking API change, or the diff doesn't do what the PR
     claims. This maps to "would warrant request-changes" — say so, but never
     submit any review event.
5. Close with: "Comments are staged as a pending review — only you can see
   them until you submit or discard on GitHub."

## Edge cases

| Situation | Behavior |
|---|---|
| Existing pending review | Ask replace/abort (Step 1); `--replace-pending` only after explicit approval. |
| Zero findings | No review created; verdict still reported. |
| Huge PR | Shard; cap agents; disclose coverage; verdict ≤ neutral if partial. |
| Fork PR | `OWNER/REPO` from PR url; never checkout; review is API-only. |
| Own PR | Pending reviews on your own PR work; no special case. |
| New push mid-review | Stage script re-anchors against the refreshed head automatically. |
| 422 after retry | Report findings chat-only with the verbatim API error; don't loop. |
