---
name: brainstorming
description: >
  Design gate before non-trivial code — proportional to blast radius and
  reversibility, the deliberate opposite of an unconditional gate. Contained,
  easily reversible changes clear with one sentence of rationale; changes with
  wide blast radius, hard to reverse, or that introduce new surface need
  genuine, viable alternatives with trade-offs weighed before any code gets
  written. Produces the chosen approach and its trade-offs, sized to the gate,
  handed off to leo:writing-plans.
when_to_use: >
  Before starting non-trivial code: a new feature, a new integration surface,
  a schema or data-model change, anything that's expensive or awkward to
  undo. NOT for a contained, easily reversible tweak (that just needs one
  sentence of rationale, not this skill's full procedure), NOT for pure
  investigation (use investigator), and NOT for writing the plan itself
  (leo:writing-plans) — brainstorming stops at a chosen approach, it never
  slides into implementation.
---

# brainstorming

Core rule: the depth of the design gate is proportional to blast radius and
reversibility, not to how the task felt when it landed. A one-line change to
a private helper does not need three alternatives; a new public API or a
schema migration does.

## Size the gate first

Before generating anything, classify the change:

- **Contained + easily reversible** (a local refactor, an internal helper, a
  flag you can flip back) → one sentence of rationale is enough. Say what
  you're doing and why, then move to leo:writing-plans or straight to
  implementation per the routing table.
- **Wide blast radius, hard to reverse, or new surface** (public API, schema
  or data-model change, cross-service contract, anything users or other
  systems will come to depend on) → full gate: genuine alternatives with
  trade-offs, written down, before any code.

When unsure which bucket, treat it as the wider one — the cost of one extra
paragraph is nothing next to the cost of an unreversible wrong turn.

## Alternatives must be viable

Every alternative in a full gate has to be something a reasonable engineer
could actually ship and defend, not a strawman stood up to make the first
idea look good by comparison. If you can't articulate a real reason someone
would pick alternative B, it isn't an alternative — go find one that's
actually competing for the job, or drop down to the one-sentence gate because
there's really only one sane approach.

Test: could you argue for this option in front of Leo without a "but
obviously we won't do this" tone? If not, it's a strawman — cut it.

## Generation method

To surface genuinely different options, vary along a different axis each
time rather than producing three cosmetic variants of the same idea:

1. **Data model vs. control flow vs. boundary/interface** — would this
   problem look different if you moved the complexity into the data shape,
   into how execution flows, or into where the interface/boundary sits?
2. **Prior art in the repo** — grep for how this repo already solved a
   similar problem (via Explore, not inline digging) and steal that pattern
   before inventing a new one. Consistency with existing structure is a real
   trade-off, not a tie-breaker of last resort.
3. **The 10x-simpler version** — what would this look like with one-tenth
   the code/config/moving parts? Even when you don't ship it, it's usually
   the sharpest lens on what the "proper" version is paying for.

## Output

- Contained/reversible: one sentence of rationale, folded into the plan or
  the commit itself.
- Full gate: the chosen approach plus the trade-offs record — a paragraph for
  a medium decision, a short doc for a genuinely high-stakes one. Sized to
  the gate, not padded to look thorough.

Either way, the output is a decision, not code. Hand it to
leo:writing-plans for the actual plan; brainstorming never slides into
implementation itself.

## Self-talk to catch

- "I'll just list two options so it looks considered" — if you can't defend
  both, that's a strawman, not a gate.
- "This is a big change but I already know the answer" — blast radius and
  reversibility decide the gate size, not your confidence.
- "I'll sketch the plan while I'm at it" — that's leo:writing-plans' job;
  stop at the chosen approach.
- "One sentence feels thin for something this exciting" — excitement isn't
  blast radius; if it's contained and reversible, one sentence is correct.

## Escalation

Planning-tier work runs at Opus per the routing table (plan mode in an Opus
session, or the `planner` subagent otherwise). Escalate per the standard
ladder: two failed passes at reaching a defensible set of alternatives step
up a tier; a genuine deadlock between two Opus-tier framings goes to
`expert`, announced in one line, never silently.

## Works with

- leo:writing-plans — takes the chosen approach and turns it into an
  executable plan.
- investigator — for questions that need evidence before a design question
  can even be framed.
- Explore — cheap prior-art search feeding the generation method above.
