---
description: Cost-aware delegation: keep small work local and select the cheapest competent worker.
alwaysApply: true
---
# Cost-aware delegation

Split the task into steps before delegating. Keep a step local when it is small
or your next step depends on its result. Delegate a step when it is independent
of your next step or its tool output would swell your context: bulk reading,
review lenses, separable changes. Do not repeat finished investigation to hand it
off. Raise effort before spawning.

Gate every delegation on verifiability: name the check that proves the result
(test, command, diff, grep). If nothing can check it, keep it local.

Choose the cheapest tier whose result that check would catch:
- Cheap: bounded retrieval, mechanical changes, straightforward checks.
- Standard: ordinary debugging, implementation, tests, and scoped review.
- Premium: difficult diagnosis, cross-module design, complex changes, and consequential review.
- Parent-level: exceptional; only when premium is insufficient and the work is
  independent. Otherwise keep it in the parent.
Route by ambiguity, consequence, and verifiability, not size. When a result fails
its check or the worker reports `Result: escalate`, re-dispatch one tier up with a
brief that begins `Escalation from <tier>:` and states the failure; never retry
the same tier.

<!-- leos-agent:routing -->
Claude defaults: Haiku/Sonnet/Opus/current parent. Codex defaults: Luna/Sol/Astra/current
parent. Other harnesses require configured cheap/standard/premium tiers. Select a model
or native tier profile only through supported fields.
<!-- /leos-agent:routing -->

Never deliberately choose a child more expensive than the parent. The guard
compares offline reference prices; unknown or ambiguous prices are allowed and
reported, not proof of savings. Tiers do not override this ceiling.

A brief gives a bounded goal, starting paths, settled decisions, tools needed,
the check, and a result contract. Prefer fresh context; on Codex use
`fork_turns="none"` where supported. Do not copy history or repeat returned work.
Batch independent tool calls; parallel work must justify its integration cost.

Workers do their own work without further delegation; PR review's reviewer may
use bounded lenses under the review skill. Work that outlasts one context
continues by handoff to a fresh session, not a deeper tree. Load workflow skills
only when needed; keep volatile state and price catalogs out of always-loaded
text. Verify the final result yourself with evidence; report uncertainty and
incomplete coverage. Cache behaviour varies; no token or dollar threshold
guarantees savings.
