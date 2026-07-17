---
name: delegation
description: >
  Operational mechanics for dispatching subagents — a single spawn or a large
  fan-out — the companion to the policy's "Delegate the labor" section.
  Covers brief construction, model/effort pinning, the four-state return
  contract, and ledger-backed progress tracking for long multi-agent runs.
when_to_use: >
  Any time work is routed to a subagent (Explore, investigator, executor,
  implementer, reviewer, expert) rather than done inline — single dispatch or
  fan-out. NOT for deciding *which* tier a task belongs in (that's the
  routing table in CLAUDE.md); this skill covers what to do once the tier is
  already chosen.
---

# delegation

Core rule: a subagent gets one shot at the brief and no session history. If
the brief doesn't stand alone, the dispatch is already broken.

## Writing the brief

Every dispatch is self-contained: goal, constraints, exact file paths, the
checks to run, and what the return must contain. Write it as if for a
stranger who will never see this conversation — because that's what a
subagent is. A brief missing a file path or a check produces a report that
looks done and isn't.

Bad: "fix the flaky auth test." Good: "`tests/auth/session_test.py::test_expiry`
fails intermittently (repro: run it 20x, ~1 in 8 fails). Fix the race, keep
the test's intent unchanged, don't touch other tests in the file. Run
`pytest tests/auth/session_test.py -x` 20 times clean before reporting done.
Return: files touched, the race you found, the check output." The second
version needs no follow-up question; the first invites three.

## Pin model and effort

Every dispatch pins **model AND effort** from the routing table — opus for
judges (reviewer, investigator), sonnet for execution (implementer, executor
on normal work), haiku for mechanical work (executor on boilerplate). expert
never appears in a fan-out — one at a time, never fanned. An unpinned call
silently inherits the session's tier: in an opus session that means every
executor spawn quietly runs at opus, and a ten-item fan-out burns
opus-fan-out money for haiku-shaped work. Pin both fields on every spawn, not
just the ones that "obviously" need it.

## The four-state return contract

A subagent's report must resolve to exactly one of four states. Don't accept
a report that hedges across two of them.

| State | Means | Your response |
|---|---|---|
| `done` | Work finished, matches the brief | Verify against artifacts — see leo:verification — never take the self-report at face value |
| `concerns` | Finished, but flags something worth a second look | Read the concerns before accepting; they're often the real finding |
| `needs-context` | Blocked on missing information you can supply | Supply it and re-dispatch **once** — a second needs-context on the same gap means the brief itself is broken, escalate the tier |
| `blocked` | Blocked on something you can't hand over inline | Resolve the blocker, or escalate per the ladder — never a silent same-tier retry |

`needs-context` and `blocked` look similar; the test is whether the missing
piece is something *you* hold (needs-context — a file path, a decision, a
credential) or something neither of you can supply without more work
(blocked — a failing external service, a genuinely ambiguous requirement).

## Long multi-agent runs: the ledger

A run spanning many dispatches survives context compaction only if progress
is persisted outside the conversation. Use
`${CLAUDE_PLUGIN_ROOT}/scripts/state.py` (get / merge / path — flock-guarded,
atomic writes, keyed per repo) as the ledger, not ad hoc notes in the
transcript. Each entry: item id, status (one of the four states above, plus
`pending` / `in-progress`), artifact path (branch name, file, or diff). On
resume, read the ledger first — anything already `done` or `concerns` is not
re-dispatched; anything `blocked` is reported, not silently retried.

A ledger entry is small — `{"items": {"<id>": {"status": "done", "artifact":
"branch:fix/eng-123-slug"}}}` merged via `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/state.py"
merge <skill-name> <owner/repo> '<patch>'` — but it's the only thing standing between a
compaction mid-run and forty items silently re-dispatched from item 1.
Update it after every dispatch resolves, not in a batch at the end: a crash
between "agent finished" and "ledger written" is exactly the gap this
exists to close.

For a batch of independent, well-scoped fixes, don't hand-roll this loop —
the reusable workflow at `${CLAUDE_PLUGIN_ROOT}/workflows/cost-tiered-fix.js`
(Workflow tool, `scriptPath`) already implements plan → tiered execute →
opus verify with escalation built in, including its own progress tracking.
Reach for it before writing a bespoke fan-out loop; write the ledger
approach above only when the run doesn't fit that workflow's shape (e.g. one
dispatch at a time inside a larger interactive flow, not a clean batch).

## Parallel dispatch: own your files

Fan-out is safe only when each spawn writes to **disjoint** files — no two
concurrent dispatches touching the same path. If the work can't be split
into disjoint file sets (one coherent change that happens to span many
files, like a single ticket fix), don't fan out — either run it sequentially
in one dispatch, or give each spawn its own isolated tree via leo:worktrees
so parallel edits can't collide even when the file sets overlap.

## Self-talk to catch

- "I'll skip pinning effort, model is enough" — no; an unpinned effort on an
  opus judge still runs at opus prices, at auto effort, which is not what
  the routing table costed out.
- "The brief is short, they'll infer the rest" — a subagent infers nothing;
  it has this brief and nothing else.
- "It said needs-context, I'll just re-ask the same way" — re-dispatching
  with the identical brief reproduces the identical gap; either add the
  missing piece or step up a tier.
- "Two spawns editing the same file will probably be fine, they touch
  different functions" — same file is not disjoint; sequence them or
  isolate with a worktree.
- "This ten-item fan-out is basically cost-tiered-fix, I'll just write the
  loop myself" — the workflow already handles escalation and orphan
  tracking; reinventing it inline drops that for no reason.

## Works with

- leo:verification — how a `done` report gets checked against real
  artifacts, not trusted as stated.
- leo:worktrees — file isolation for parallel dispatches that can't be made
  disjoint by scope alone.
