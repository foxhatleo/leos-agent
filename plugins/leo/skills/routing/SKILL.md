---
name: routing
description: >
  Leo's operating policy for tiering and delegating work. Read it before
  writing code, choosing a model, dispatching a subagent, fanning work out,
  or reporting a task finished. Covers which model and reasoning effort each
  kind of work runs at, when to escalate instead of retrying, what must be
  delegated rather than done inline, the phrases that authorise a large
  fan-out, where machine-local state belongs, and an index of every other leo
  skill with the moment that calls for it.
when_to_use: >
  At the start of a coding, planning, diagnosis, review, or delegation task,
  and any time the right tier, the review requirement, or which leo skill
  applies is unsettled. Also before a fan-out, because the cost of one is
  roughly an order of magnitude above a single dispatch. Do not use for a
  question that changes no files and spawns nothing.
---

# Routing

Tier the work, not the session. A request that spans phases — "find out why
this breaks, then fix it" — is two tiers, and splitting it is the first
decision, not an afterthought.

Tier names below are roles, not model names. Which concrete model and effort
each one means here, and which of them your harness already provides natively,
are in [references/harnesses.md](references/harnesses.md). Read your own
section of that file once per session; the rest of it describes machines you
are not on.

## Tiers

| Kind of work | Verbs that signal it | Tier | Effort |
|---|---|---|---|
| Locating code, mapping structure | where is, find, list, which file | Haiku | low |
| Mechanical change | rename, codemod, reformat, apply a known pattern | Haiku | low |
| Normal implementation | implement, fix, build, refactor, carry out the plan | Sonnet | medium |
| Structured extraction from a diff | summarise findings for a judge | Sonnet | medium |
| Diagnosis that ends in a verdict | why does, diagnose, root-cause, trace | Opus | high |
| Design and planning | plan, design, architect, decide between | Opus | high |
| Judging a change | review, verify, audit | Opus | high |

Effort is half the tier. A judge at low effort is a cheap opinion with an
expensive price tag; a mechanical edit at high effort pays for deliberation
nobody asked for. Where the harness lets a dispatch choose both, choose both.
Where it fixes them per role, that is already done for you. Where it offers
neither, the tier is advice and you should say so rather than implying a
pin that does not exist.

**Escalate rather than grinding.** A cheap-tier task that turns ambiguous, or
fails twice, moves up one rung — it does not get a third attempt at the same
rung. When the right tier is genuinely unclear, start one rung high; the
wasted spend on a single over-tiered task is smaller than the wasted spend on
three under-tiered attempts. Opus is the ceiling. Past it, stop and report
what you know and what would settle it, rather than looping.

## Done means reviewed

Every change to code carries a review phase whether or not anyone asked for
one. Writing the change is not the end of the task; a clean verdict on the
actual diff is. The contract for that gate — what counts as reviewed, the two
narrow exemptions, and how to run it on your harness — is leo:review-gate.
Reach for it before reporting anything as done, fixed, or passing.

## Delegate the volume

The main loop decides; delegated work does the reading and typing. In an
expensive session, doing bulk work inline bills the whole pile at the judge's
rate.

- Locating code and mapping structure goes to the cheap read-only scout, in
  parallel when the questions do not depend on each other.
- Mechanical edits go to the cheap writer, fanned across items that touch
  different files.
- Normal implementation goes to the middle tier, handed the plan.
- Diagnosis that needs a verdict goes to one expensive read-only agent per
  question. Distinct questions may run at once; the same question never gets
  two, because two answers to one question is a tie, not a confirmation.
- Judging a diff goes to the review gate.

Scale to the shape of the problem: one lookup is one dispatch, comparing a
handful of areas is a handful, and anything larger is a fan-out that needs the
authorisation below. In an expensive session this is firm rather than
advisory — more than a few inline edits, or more than a handful of inline
searches, means the work should have gone out. The exception is a single small
touch where writing the brief would cost more than making the change.

Fan out only across items that write to different files. Two agents editing
one file is a lost edit, not parallelism; sequence those, or give each its own
tree. Dispatch mechanics — how to write a brief that survives having no
conversation history, the four states a report may resolve to, and how to keep
progress durable — are leo:delegation.

## Authorising a fan-out

These phrases are standing permission for multi-agent orchestration: **"fan
this out"**, **"workflow this"**, **"grind on this"**, **"do this properly"**.

Without one, for work that looks large, offer orchestration in a single line
naming the rough agent count and model mix, then proceed with one agent unless
the offer is taken. A large fan-out never starts silently, because its cost is
roughly an order of magnitude above the single dispatch it replaces.

## Machine-local state

Anything a skill or role must remember across dispatches goes to
`$LEOS_AGENT_LOCAL_PATH`, defaulting to `~/.leos-agent-local`. Top-level keys
are `owner/repo`, or the absolute project path when there is no repository, so
data never leaks between projects. Go through `scripts/state.py` (`get`,
`merge`, `path`) rather than hand-rolling read-modify-write — it is
flock-guarded and writes atomically. Where the harness owns a task list of its
own, that list is the better ledger for in-flight progress; the harness
reference says which applies.

## Skill index

Reach for the one that matches the moment. Each encodes mechanics this policy
assumes, sized to the work rather than added as ceremony.

| At this point | Consult |
|---|---|
| Before claiming done, fixed, or passing | leo:review-gate |
| A bug or failing test, before any fix | leo:debugging |
| An approach not yet chosen, before non-trivial code | leo:brainstorming |
| Turning a chosen approach into a plan | leo:writing-plans |
| Carrying out a written plan | leo:executing-plans |
| Adding or changing runtime behaviour | leo:test-first |
| Coding against someone else's API | leo:freshness |
| Evidence behind a completion claim | leo:verification |
| A change someone will look at | leo:visual-verification |
| Dispatching subagents or a fan-out | leo:delegation |
| Isolating branch work | leo:worktrees |
| Landing or cleaning up a finished branch | leo:finishing-a-branch |

Invoked by name rather than reached from the table: `leo:review-pr`,
`leo:resolve-ticket`, and `leo:watch-review`. One more, `leo:attach-pr`, ships
only on Claude Code — the harness reference lists what else differs there.
