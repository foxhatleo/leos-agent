---
name: fix
description: >
  End-to-end ticket fix: resolve the ticket (Linear or Jira), pull linked
  context (Confluence, Slack, GitHub), investigate and plan at Opus tier, get
  Leo's explicit sign-off, implement on a worktree branch with sonnet/haiku
  executors, Opus-review the diff, then push and open a DRAFT pull request in
  the browser.
when_to_use: >
  Leo asks to fix or implement a specific tracked ticket by ID ("fix ENG-123",
  "/fix PLAT-42"). NOT for ad-hoc fixes with no ticket (normal
  execute-then-review flow) and NOT for batches of independent items (that is
  the cost-tiered-fix workflow).
argument-hint: "[ticket-id]"
model: opus
allowed-tools:
  - Bash(gh *)
  - Bash(git *)
  - Agent
  - AskUserQuestion
  - EnterWorktree
  - ExitWorktree
  - WebFetch
---

# /fix — ticket to draft PR

Tier map: this main loop (opus) triages, plans, gates, and synthesizes;
`investigator` (opus) diagnoses; `executor` implements (haiku for mechanical
steps, `model: sonnet` override for normal ones); `reviewer` (opus) judges the
diff before anything is pushed.

Hard rule: **nothing is created — no worktree, no branch, no edit — before Leo
approves the plan in Step 4.** Steps 0–3 are read-only.

## Preflight (injected)

- Auth: !`gh auth status 2>&1 | head -3`
- Repo: !`gh repo view --json nameWithOwner,defaultBranchRef,isFork 2>&1`
- Tree: !`git status --porcelain 2>&1 | head -5`

`$0` is the ticket ID; further arguments are steering constraints ("don't
touch the API layer") that carry into investigation, the plan, and executor
specs. No ticket ID → ask for one and stop. Not a repo / gh unauthenticated →
stop with a one-line diagnosis. A dirty main checkout is fine (the worktree
isolates) — note it and continue.

## Step 1 — Resolve the ticket (Linear or Jira)

Never hardcode MCP tool names — server prefixes differ per machine; bind by
capability at runtime (a Linear issue-fetch tool; the Atlassian tools
`getAccessibleAtlassianResources` → cloudId → `getJiraIssue`). Use ToolSearch
if the tools are deferred.

1. **Known prefix**: the global CLAUDE.md has a `## Ticket sources` prefix →
   tracker table (project CLAUDE.md may override). If the ticket's prefix is
   mapped, go straight to that tracker.
2. **Unknown prefix**: probe whichever tracker MCPs are connected. Exactly one
   hit → use it, then ask via AskUserQuestion whether to remember the mapping.
   Both hit, or ambiguous → AskUserQuestion with the two titles; Leo picks.
   Either way, **persist the mapping**: append the row to the table in
   `~/.leos-agent/claude/CLAUDE.md` (the repo copy — never the machine-local
   stub) and remind Leo to commit the repo.
3. **No tracker reachable**: tell Leo which MCP is missing and the remedy
   (`~/.leos-agent/install.sh mcp`, or
   `claude mcp add --transport http linear-server https://mcp.linear.app/mcp`,
   then `/mcp` to authenticate), and offer: paste the ticket text to continue,
   or abort. Never guess ticket content.

Normalize the result: `{id, url, title, body, acceptance criteria, recent
comments, links[]}`. Fetch the ticket's comments too — that's where
constraints and prior attempts hide.

## Step 2 — Linked resources (best-effort, never fatal)

Collect URLs from the ticket body, comments, attachments, and (Jira)
`getJiraIssueRemoteIssueLinks`. Then per link:

- **Confluence page** → `getConfluencePage` (Atlassian MCP). Pages over ~200
  lines: don't read here — spawn a sonnet summarizer subagent that returns a
  tight summary plus load-bearing quotes.
- **Slack permalink** → Slack MCP is assumed connected and authenticated.
  Parse `…/archives/<CHANNEL_ID>/p<digits>` → channel ID + `thread_ts`
  (insert the decimal point 6 digits from the right: `p1700000000123456` →
  `1700000000.123456`) and read the thread. **If no Slack MCP is connected,
  tell Leo explicitly** ("Slack MCP isn't set up on this machine — see README
  › MCP servers") and continue without it.
- **GitHub PR/issue/commit** → `gh` view commands.
- **Anything else** → WebFetch, one attempt.

Every failure or skip goes into a **context-gaps list** shown at the sign-off
gate — Leo sees exactly what wasn't read before approving.

## Step 3 — Investigate (opus)

Spawn `investigator` subagents with an explicit `model: opus` override —
default **2 in parallel**: (a) *code path*: where the change lives, exact
files/lines, reproduction reasoning, current test coverage; (b) *history &
blast radius*: git archaeology, related PRs, callers/consumers of what will
change, landmines named in ticket comments. Scale down to 1 when the ticket
names the file and fix; up to 3 max for gnarly cross-cutting work — never
more. Feed them the normalized ticket, resource summaries, and Leo's steering
constraints; let cheap `Explore` scouts handle raw searching. Synthesize root
cause and approach here.

## Step 4 — Plan and sign-off gate

Present a plan of ~20 lines:

1. **Ticket** — id, title, one-line restatement of the ask.
2. **Root cause / approach** — 2–4 lines with `file:line` evidence.
3. **Change list** — files to touch, what changes in each, executor tier per
   step (haiku/sonnet).
4. **Test plan** — checks to run, tests to add.
5. **Risks & context gaps** — including every unread link from Step 2.
6. **Branch**: `fix/<TICKET-ID>-<kebab-slug>` (slug ≤ 40 chars).

Then AskUserQuestion: **Approve** / **Adjust** (free-text; revise and re-gate,
looping until approve or abort) / **Abort** (nothing was created; clean exit).

## Step 5 — Worktree

Only after Approve: `git fetch origin`, then EnterWorktree and create branch
`fix/<TICKET-ID>-<slug>` off `origin/<defaultBranch>`. Fallback if the tool is
unavailable: `git worktree add -b fix/<id>-<slug> ../<repo>-fix-<id>
origin/<default>` and work by absolute paths.

Executors in Step 6 must **NOT** use `isolation: worktree` — this is one
coherent change in one shared tree (unlike cost-tiered-fix's independent
items).

## Step 6 — Execute (sonnet/haiku)

Per plan step:

- Mechanical, fully specified → `executor` as-is (haiku).
- Normal implementation → `executor` with `model: sonnet` override — the plan
  already contains exact specs, so the executor contract (do exactly this,
  stop on ambiguity) is right.
- Steps touching disjoint files run as parallel spawns; dependent steps
  sequential. Executors commit as they go.
- This loop implements directly only for trivial diffs (< ~10 lines) where
  writing the spec would cost more than the change.
- Escalate, don't struggle: an executor reporting ambiguity or failing twice →
  redo that step one tier up (haiku → sonnet → opus). Never retry in place.

Then run the project's real check suite once (discover the command from
package.json / Makefile / CI config). Failures become new executor fix steps;
two failures on the same step → escalate its tier; still red → carry it to the
Step 7 gate as a known failure, never silently.

## Step 7 — Mandatory opus review

Spawn a **fresh** `reviewer` subagent (never self-review — this loop wrote the
plan and is biased toward believing it worked). Give it: the normalized
ticket, the approved plan, and the diff scope
`git diff $(git merge-base origin/<default> HEAD)...HEAD`.

- Blocking findings → each becomes a sonnet executor fix task → re-review the
  delta (reviewer gets prior findings + new diff). **Max 2 rounds.**
- Still blocking after round 2 → AskUserQuestion: **Push anyway as draft**
  (PR body gains a "Known issues" section listing the findings) / **Abort**
  (branch and worktree left local; report the path).
- Non-blocking findings ride along into the PR body's review notes.

## Step 8 — Ship

1. `git push -u origin fix/<TICKET-ID>-<slug>`. Fork setups (preflight
   `isFork`): push to the fork, create the PR against upstream with
   `gh pr create -R <upstream> --head <user>:<branch> …`.
2. `gh pr create --draft -B <defaultBranch> -H <branch> -t "[TICKET-ID] <title>" -b <body>`
   with body sections: **Summary** (2–3 lines) · **Ticket** (link; for Linear
   also a bare `Fixes <TICKET-ID>` line so Linear auto-links) · **Approach**
   (from the approved plan) · **Test plan** (checks actually run + results) ·
   **Review notes** (non-blocking findings / known issues) · **Context gaps**.
   Same voice rules as /review-pr: no filler, no emoji, no self-praise.
   If a PR already exists for the branch, open that one instead and say so.
3. `gh pr view --web` to open it in the browser.
4. ExitWorktree. Do **not** write back to the ticket (no comment, no status
   transition) — deliberate non-action; Leo asks separately if he wants it.
5. Final report: branch, PR URL, worktree path (left in place for follow-ups),
   checks run, review rounds used, remaining non-blocking notes.

## Failure paths

| Failure | Behavior |
|---|---|
| Ticket not found in any source | Paste-ticket-text or abort; never guess content. |
| Same ID resolves in two trackers | AskUserQuestion with both titles. |
| No tracker MCP connected | Report the missing MCP + remedy; paste-or-abort. |
| Slack MCP absent | Tell Leo it isn't set up; continue with a context gap. |
| Confluence/other link unreadable | Skip; record in context gaps. |
| Tests fail during execution | Fix loop with tier escalation; surface if still red. |
| Review blocks twice | Gate: push-with-known-issues vs abort. |
| Push rejected / no permission | Report; suggest fork flow; leave branch local. |
| Abort at the sign-off gate | Nothing was created. After the worktree exists: branch + worktree left local, path reported. |
