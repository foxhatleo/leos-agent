---
name: writing-plans
description: >
  Quality bar for plans produced by the planner agent or in plan mode. A
  plan is done when a Sonnet implementer can execute it without making a
  single design decision — every step names exact files, shows literal
  code or commands, and states how to verify it, anchored to a recorded
  base ref.
when_to_use: >
  Writing or reviewing a plan before handoff to leo:executing-plans —
  planner-agent output, plan-mode output, or any multi-step change spec.
  NOT for choosing the approach itself (that's leo:brainstorming) and NOT
  for the implementation or review phases that consume the plan.
---

# writing-plans

Core rule: a plan is done when a Sonnet implementer can execute it without
making a single design decision. If executing the plan requires judgment
calls, the plan isn't finished — it's a to-do list wearing a plan's clothes.

## When this fires

Any time a plan is about to be handed off for execution: planner-agent
output before leo:executing-plans picks it up, plan-mode output before
approval, or a plan Leo asks you to review. Not for the design discussion
that precedes the plan — an unsettled approach means back up to
leo:brainstorming (rule 5 below), not push forward into more plan detail.

## The five load-bearing rules

1. **Exact files, literal code, stated verification.** Every step names the
   file(s) it touches, shows the literal code or command to write/run — not
   a description of what the code should do — and states how to verify the
   step worked (a command, a test name, an expected output). "Add error
   handling to the parser" is not a step. "In `src/parser.py`, wrap the
   `json.loads(raw)` call on line 42 in a `try/except json.JSONDecodeError`
   that raises `ParseError(f\"bad payload: {raw[:80]}\")`; verify with
   `pytest tests/test_parser.py::test_malformed_json`" is a step.

2. **Base ref in the header.** The plan header records the base ref —
   `git rev-parse HEAD`, or literally "uncommitted working tree" if the
   plan starts from dirty state. Without a shared base ref, implementer and
   reviewer are diffing against different worlds and neither's output
   means anything to the other.

3. **No placeholders.** "TBD", "TODO", "handle edge cases", "add
   validation", "similar to step N" are plan failures, not acceptable
   shorthand — fix them before handoff, not during execution. A
   placeholder in a plan just moves the design decision onto whichever
   Sonnet implementer hits it first, which is exactly the failure mode
   this skill exists to prevent. "Similar to step N" is the sneakiest
   form: it looks concrete but hides a judgment call about what actually
   differs — write the step out.

4. **Steps sized to a reviewable boundary.** Each step should be the
   smallest unit a reviewer could accept or reject on its own — one file's
   worth of change, one migration, one function. Bundle unrelated changes
   into a single step and the reviewer either rubber-stamps the whole
   thing or blocks all of it over one bad line. If a step needs "and
   also" to describe, it's two steps.

5. **Unsettled approach → back up.** If writing the plan surfaces a real
   design fork ("could go with polling or webhooks here") that the plan
   author is resolving on the fly, stop — that's not plan-writing, that's
   design happening inside a document meant to record decisions already
   made. Route to leo:brainstorming to settle the approach, then come back
   and write the plan. A plan for an unchosen design is waste: the
   implementer either can't proceed or silently picks for you, and now
   the review is judging a decision nobody signed off on.

## Self-talk to catch

- "The implementer will know what I mean" — no placeholder survives
  contact with a different model on a different day; write the literal
  code.
- "This step is basically step 3 again" — then step 3's text belongs
  here too, verbatim or adapted; "similar to step 3" is a placeholder.
- "I'll figure out the base ref when review starts" — the header needs it
  now, or implementer and reviewer silently diff against different trees.
- "It's obviously going to be small, I don't need to size the step" —
  size it anyway; "obviously small" is exactly the case where a bundled
  step slips a real decision past review.
- "I'm not 100% sure webhooks vs. polling, I'll note it as a decision
  point in the plan" — a decision point in a plan is a design fork that
  belongs in leo:brainstorming, not a step for the implementer to guess
  at.

## Works with

- leo:brainstorming — resolve the approach before a plan gets written for it.
- leo:executing-plans — consumes a plan that passes this bar; if it can't
  find exact files/commands or hits a placeholder, the plan should have
  failed this checklist.
- reviewer — judges the diff the plan produced, using the same base ref
  the plan recorded.
