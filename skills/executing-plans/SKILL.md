---
name: executing-plans
description: >
  Checkpoint discipline for carrying out a written plan — batch execution
  with a check at every batch boundary, plan-intent-wins-on-architecture /
  reality-wins-on-mechanics arbitration, and one fix-then-re-review cycle
  before stopping to report. Used by the implementer agent, or the main loop
  when it executes a plan directly.
when_to_use: >
  A written plan (from planner, an issue, or Leo's own outline) is about to
  be turned into code. NOT for open-ended implementation with no plan
  (normal execute-then-review flow) and NOT for the review step itself
  (the reviewer agent judges the diff; this skill only carries out the plan).
---

# executing-plans

Core rule: a plan is executed in checkpointed batches, never as one long
uninterrupted run. Each checkpoint is a place execution is allowed to stop
without having made things worse.

## Before edit one

Sanity-check the plan against the tree it's about to touch:

- Base ref matches what the plan assumed — `git rev-parse HEAD` against the
  base the plan was written against. Drifted → say so before touching
  anything; the plan may already be stale.
- Files/symbols the plan names actually exist at the paths/shapes it
  describes. A plan step that references a function that moved or a file
  that's gone is a stop-and-report, not a guess-and-proceed.

This is cheap — a few Read/Grep calls — and skipping it is how a plan
written against yesterday's tree silently corrupts today's.

## Execute in batches

Break the plan into batches along its own natural seams (usually: one
plan-step or one cohesive file group per batch). At each batch boundary:

1. Finish the batch's edits.
2. Run the narrowest relevant checks for what that batch touched — the
   touched test file, a targeted typecheck, not the full suite every time.
3. Green → advance to the next batch. Red → stop the batch right there; fix
   it or report it. Never carry a red check into the next batch hoping it
   resolves itself — a checkpoint exists precisely to catch this before the
   failure compounds across three more batches of edits built on top of it.

This is the same shape as leo:delegation's tiering: cheap, frequent checks
bound the blast radius so the expensive step (review) isn't debugging a
pile of unrelated regressions.

## Plan intent wins on architecture; reality wins on mechanical detail

Two different kinds of mismatch between plan and tree call for two different
responses:

- **Mechanical drift** (a renamed variable, a moved file, a slightly
  different function signature than the plan assumed) — reality wins. Adapt
  the mechanics silently and keep going; that's normal execution, not a
  deviation worth flagging.
- **Architectural disagreement** (the plan's approach doesn't fit the actual
  structure, a step contradicts how the system actually works, following it
  as written would build on a wrong premise) — the plan's intent still wins
  over improvising a fix, but only the plan's author can resolve a real
  conflict. Stop and report the disagreement; never silently redesign around
  it. Silent redesign is worse than executing a flawed plan, because it
  hides the disagreement instead of surfacing it.

When genuinely unsure which kind of mismatch it is, treat it as
architectural and stop — reporting an unnecessary pause costs a message;
silently redesigning costs trust.

## Behavior changes still default to test-first, done still means verification

A plan step that changes behavior doesn't get a pass on process because it's
already written down. Default to leo:test-first for those steps, and treat
"the plan is implemented" and "the plan is done" as different states — done
still means the change clears leo:verification, not just that every step got
executed.

## One fix-then-re-review cycle

Once all batches are in, this hands off to the standard review gate — spawn
`reviewer` on the actual diff against the recorded base ref,
with the plan text as the original request. If it comes back with blocking
findings: fix at the executing tier, then re-review only the fix. That's
**one fix-then-re-review cycle**, full stop. A second block on the same
findings means stop the loop and report to Leo with options, expert
arbitration (the `expert` agent) among them — never a third pass, never quietly
loosening what counts as blocking to escape the loop.

## Delegation and workspace boundaries

Executing a written plan is `implementer`'s job per leo:delegation — the
main loop only executes inline when it's already the implementer context or
the touch is genuinely trivial. If the plan spans a branch of nontrivial
size, it runs on a dedicated branch per leo:worktrees, and finishing it
follows leo:finishing-a-branch rather than improvising a merge/cleanup
sequence at the end.

## Self-talk to catch

- "The plan says step 4, I'll just push through to step 7 before checking
  anything" — that's skipping checkpoints, not saving time; a break at step
  5 now costs one batch's rework instead of three.
- "This isn't quite what the plan says but it's obviously what they meant" —
  if it's mechanical, fine; if it's architectural, that's the silent
  redesign this skill exists to block. Report it instead.
- "The re-review still isn't clean but it's close enough" — close enough on
  a second block is the definition of stop-and-report, not a third fix.

## Works with

leo:test-first, leo:verification, leo:delegation, leo:worktrees,
leo:finishing-a-branch — plus the `reviewer` and `expert` agents.
