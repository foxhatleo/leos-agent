---
name: debugging
description: >
  Root-cause-before-fix loop for bugs, failing tests, crashes, and surprising
  behaviour. Five phases — Reproduce, Localize, Hypothesize, Prove, Fix — each
  with an exit criterion, so no fix lands before the cause is pinned to
  file:line. Diagnosis is read-only; the fix is a separate pass.
when_to_use: >
  Any defect, failing test, stack trace, or "why does this happen" before a
  fix is proposed. Not for planned feature work with no defect, not for
  judging someone else's diff, and not a replacement for leo:verification
  afterwards.
---

# debugging

Core rule: no fix before the cause is REPRODUCED and LOCATED at file:line. A
symptom going away is not proof — it's a coincidence until the loop below
says otherwise.

## When it fires

Bug reports, failing tests, crashes, stack traces, flaky behavior, "this
should work but doesn't." Route the diagnosis itself through `investigator`
(Opus, read-only) per the model-routing table — this skill is its loop.
Doesn't fire for greenfield feature work (no defect exists yet) or for
diffing someone else's change (that's leo:review-gate).

## The five phases

Named exactly, run in order, each with an exit criterion. Do not skip a
phase because the bug "looks obvious" — obvious bugs are exactly the ones
where a wrong guess ships fastest.

| Phase | Exit criterion |
|---|---|
| **Reproduce** | The failure fires on command — a test, a script, a repro sequence — not "worked once." No stable repro yet is itself a finding: report it, don't guess past it. |
| **Localize** | The failure is traced to a specific **file:line**, not a subsystem or a vibe ("something in auth"). Read the actual code path the repro exercises; don't infer from names or docs. |
| **Hypothesize** | One sentence: "X happens because file:line does Y instead of Z." One hypothesis at a time — write it down before touching anything. |
| **Prove** | The smallest evidence that the hypothesis IS the cause, not just correlated with it. Where the surface is testable, that's a failing test written per `leo:test-first` — red on the bug, and its assertion names the file:line from Localize. Where nothing is testable (infra, timing, external system), the next-smallest evidence: a log line, a debugger break, a minimal repro script. |
| **Fix** | The change that makes Prove's evidence pass. Happens at the routed tier (`executor` for mechanical, `implementer` for real changes) — never by the same pass that diagnosed it. |

Reproduce and Localize can compress into one step for a trivial case (a
crash with a one-frame stack trace pointing straight at the bug) — but
Hypothesize and Prove never collapse into Fix. If you catch yourself editing
code before you've written the hypothesis sentence, stop and back up.

## One hypothesis, one change

Test one hypothesis at a time. If Fix doesn't clear Prove's evidence, the
hypothesis was wrong — REVERT the change before forming the next one. Never
stack a second speculative edit on top of a first that didn't pan out; you
lose the ability to tell which change did what, and the diff stops being
reviewable. Revert, re-enter Hypothesize with what the failed attempt taught
you, and go again.

## Stuck: the ladder

After two failures on the same cause (two hypotheses tried and reverted, still
no Prove), step up one tier rather than retrying at the same one — cheap
scouting feeds a full investigator, and the investigator itself gets one
second pass with more evidence behind it. Opus is the ceiling. A genuine
deadlock there, or two verdicts on the same cause that contradict each other,
is where this stops: report the competing accounts and the evidence each
rests on, and say what observation would separate them. Don't loop a third
time at the same tier hoping the next guess lands — that's the same failure
mode as skipping Prove, just slower.

## Diagnosis and fix stay separate

The phase that reaches the verdict (Reproduce through Prove) is read-only
judge work — no edits, no reverts-of-other-people's-code, just evidence and a
file:line. Whoever ran that pass hands the hypothesis and its proof to the
executing tier for Fix. This mirrors why a reviewer never patches what it
finds: the same pass that wants to be right about the cause is a bad judge of
whether it actually is. After Fix lands, `leo:verification` and a
leo:review-gate pass on the diff are the separate checks that the fix is real and
didn't just make Prove's specific probe go quiet.

## Self-talk to catch

- "It's obviously the timeout" — obvious is not file:line; go Localize it.
- "Passing now, good enough" — passing isn't Prove; did you write the
  failing-first check, or did the symptom just stop reproducing?
- "I'll patch this and see if it helps" — that's skipping Hypothesize; name
  the mechanism before touching code.
- "One more tweak on top, I'm close" — that's the stacked-edit trap; revert
  first.
- "Third guess this tier, one more won't hurt" — it's the two-failures
  trigger; escalate instead.

## Works with

`leo:test-first` for writing Prove's failing test. `leo:verification` for
the post-Fix check. `investigator` runs this loop; leo:review-gate judges
the resulting diff once Fix is applied.
