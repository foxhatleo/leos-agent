---
name: test-first
description: >
  Failing-test-first as the default for a change to runtime behaviour. Write
  the test, watch it fail for the intended reason, then make it pass — the
  red-to-green transition is the evidence that the guard is real rather than a
  stamp added afterwards.
when_to_use: >
  Any implementation that changes runtime behaviour. Not for throwaway spikes,
  not for docs, config, or dependency bumps, and not for pure copy or styling
  — the closed exemption list is in the body.
---

# test-first

**Core rule**: before writing the change, write a failing test — and watch
it fail — for the reason the change is supposed to fix. Then make it pass.
A test that passes on its first run proves nothing about what it guards; it
could be checking the wrong thing, hitting a no-op path, or asserting
something already true.

## When it fires

Any task that changes runtime behavior: a bug fix, a new code path, a
refactor that alters observable output. If the diff can make a program do
something different, this skill applies before the diff is written.

## When it doesn't — Exemptions

A closed, named list. Outside it, the default holds — no free pass by
analogy, no "this one's basically like a spike."

1. **Spike** — throwaway exploration that gets deleted, never merged. If it
   survives into the diff, it was not a spike; go back and cover it.
2. **Docs / comments / config / dependency bumps** — no runtime behavior
   changes, nothing to guard with a test.
3. **Pure UI copy or styling tweaks** — text or CSS changes with no logic
   branch behind them.

A skip must name its exemption in the report — "skipped test-first: spike,
deleted before merge" or "skipped test-first: config only." An unnamed skip
is not a skip; treat it as coverage missing.

## Procedure

1. Write the test first, targeting the exact failure the change is meant to
   fix (the bug's symptom, or the new behavior's absence).
2. Run it. Watch it fail — and confirm it fails for the intended reason, not
   a typo, import error, or wrong assertion. A red test that fails for the
   wrong reason is as useless as one that never went red.
3. Make the change.
4. Run the test again. Green confirms the change closed the gap the red run
   opened — this red-to-green transition is the evidence, and it's the same
   evidence leo:verification asks for when confirming a change actually
   works end-to-end: don't produce it twice in different words, point to it.
5. Report which exemption applied, or report the red-then-green pair (what
   failed, what changed, what passed).

## Self-talk to catch

- "I'll add the test after, same effect" — it isn't. A test written against
  passing code never proves it can fail; you've verified the assertion
  compiles, not that it guards anything.
- "This is basically a spike" — if it's in the diff you're about to submit,
  it isn't a spike. Spikes get deleted, not merged.
- "It's small, not worth a test" — size isn't in the exemption list.
  Behavior change is the trigger, not line count.
- "I ran it and it passed, close enough" — passing without ever having seen
  it fail is not evidence. Go back and force the fail first.

## Reviewable finding

Changed runtime behavior with no test that would fail without the change is
a reviewable finding — blocking when the behavior is load-bearing (the
user-facing or system-critical path the task was actually about), otherwise
non-blocking. The reviewer checks for the red-to-green evidence, not for
test existence alone: a test that was never watched failing doesn't clear
the bar even if one exists in the diff.

## Works with

- leo:verification — shares the red-to-green transition as evidence of a
  real fix; don't duplicate the check, cite it.
- reviewer — enforces the coverage rubric line above on the actual diff.
- implementer, executor — the tiers that own writing the failing test and
  then the fix.
