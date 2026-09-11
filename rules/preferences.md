---
description: Cost-aware delegation: keep small work local and select the cheapest competent worker.
alwaysApply: true
---
# Cost-aware delegation

Decide whether to delegate before choosing a model. Keep small, tightly coupled
work local. Delegate substantial bounded work when it avoids meaningful parent
reasoning or context growth. A short brief does not imply small work.
Do not repeat completed investigation to delegate it.
Delegate independent pieces, review lenses, or bulk reading. Keep dependent
chains local; raise effort before spawning.

Choose the cheapest competent tier:
- Cheap: bounded retrieval, mechanical changes, straightforward checks.
- Standard: ordinary debugging, implementation, tests, and scoped review.
- Premium: difficult diagnosis, cross-module design, complex changes, and consequential review.
- Parent-level: exceptional; use only when premium is insufficient and independent
  work justifies delegation. Otherwise keep the work in the parent.
Route by ambiguity, consequence, and verifiability, not size.

<!-- leos-agent:routing -->
Claude defaults: Haiku/Sonnet/Opus/current parent. Codex defaults: Luna/Terra/Sol/current
parent. Other harnesses require configured cheap/standard/premium tiers. Select a model
or native tier profile only through supported fields.
<!-- /leos-agent:routing -->

Never deliberately choose a child more expensive than the parent. The guard
compares offline reference prices; unknown or ambiguous prices are allowed and
reported, not proof of savings. Model tiers do not override this ceiling.

Give a bounded goal, starting paths, settled decisions, tools needed, and a
concise result contract. Prefer fresh context; where supported on Codex use
`fork_turns="none"`. Do not copy history or repeat returned work. Batch independent tool calls;
parallel work must justify setup and integration costs.

Workers do their own work without further delegation. PR review is the explicit
exception: its reviewer may use bounded specialist lenses under the review skill.
Load workflow skills only when needed. Keep volatile state and price catalogs out
of always-loaded instructions. Verify the final result with relevant evidence;
report uncertainty and incomplete coverage. Provider cache behavior varies; no fixed token or dollar threshold guarantees savings.
