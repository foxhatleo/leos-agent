---
description: Cost-aware delegation: keep small work local and select the cheapest competent worker.
alwaysApply: true
---
# Cost-aware delegation

Decide whether to delegate before choosing a model. Keep small, tightly coupled
work local. Delegate substantial bounded work when it avoids meaningful parent
reasoning or context growth. A short brief alone does not prove a task is small.
Do not re-investigate work you already completed just to delegate it.

Choose the cheapest competent tier:
- Cheap: bounded retrieval, mechanical changes, straightforward checks.
- Standard: ordinary investigation, debugging, implementation, and review.
- Parent-level: difficult reasoning or ambiguity that warrants the parent model.

<!-- leos-agent:routing -->
Claude defaults: Haiku/Sonnet/current parent. Codex defaults: Luna/Terra/current
parent. Other harnesses require configured cheap/standard tiers. Select a model
or native tier profile only through supported fields.
<!-- /leos-agent:routing -->

Never deliberately choose a child more expensive than the parent. The guard
compares offline reference prices; unknown or ambiguous prices are allowed and
reported, not proof of savings. Model tiers do not override this ceiling.

Give a bounded goal, starting paths, settled decisions, tools needed, and a
concise result contract. Prefer fresh context; where supported on Codex use
`fork_turns="none"`. Avoid copying conversation history or repeating returned
work. Batch independent tool calls; parallel agents need enough useful work to
justify their combined setup and integration cost.

Workers do their own work without further delegation. PR review is the explicit
exception: its reviewer may use bounded specialist lenses under the review skill.
Load workflow skills only when needed. Keep volatile state and price catalogs out
of always-loaded instructions. Verify the final result with relevant evidence;
report uncertainty and incomplete coverage. Cache behavior varies by provider,
so no fixed token count or dollar amount guarantees delegation will save money.
