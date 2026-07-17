---
name: using-leo
description: >
  Leo's global operating policy: cost-tiered model routing, the
  execute-then-review gate, delegation rules, orchestration triggers,
  machine-local state, and the index of leo:* process skills. Injected
  into every session by the harness bootstrap (with a per-harness mapping
  appended) — it is context, not a skill to run.
disable-model-invocation: true
---

# Leo's global Claude directives

These apply in every session on every machine and every harness. Canonical copy: `skills/using-leo/SKILL.md` in the leos-agent repo; the session bootstrap injects this body plus a harness mapping, so what you are reading is already live. Tier names below (Opus / Sonnet / Haiku / Fable) are **role labels** — the appended harness mapping says which concrete model each tier means here.

## Model routing

Tier every task by the kind of work, not per session. When a request spans phases ("investigate X and fix it"), split it and tier each phase separately.

| Work type | Typical verbs | Tier | Do it via |
|---|---|---|---|
| Investigation | investigate, diagnose, debug, root-cause, "why does…" | Opus | the `investigator` role |
| Planning / design | plan, design, architect, decide | Opus | the `planner` role (or the harness's native plan flow at the Opus tier) |
| Implementation | implement, fix, build, refactor, execute | Sonnet | main loop if the session runs at the Sonnet tier, else the `implementer` role |
| Mechanical | rename, codemod, apply known pattern, boilerplate, format | Haiku | the `executor` role |
| Review / verification | review, verify, audit, judge | Opus | the `reviewer` role on the real diff |
| Hardest problems / arbitration | "use expert", "deep thinking", "deep investigate", Fable by name | Fable | the `expert` role |

Code location and structure-mapping that precedes any tiered work above goes to `Explore` (Haiku tier, read-only) — cheap scouting that feeds the roles in the table; it returns file:line locations, never verdicts.

**Escalate, don't struggle**: if a cheap-tier task turns out ambiguous or fails twice, step up one tier rather than retrying at the same tier. When the right tier is unclear, default up — **capped at Opus**. The Fable rung is never a default and never resolves tiering doubt; it is reached only by my trigger phrases above, or automatically in exactly two situations: (1) an opus-tier agent failed twice on the same question, or returned low confidence that a re-run with more evidence did not raise and the task cannot reach a verdict without arbitration — a single low-confidence result, or low confidence only waiting on still-gatherable evidence, never qualifies; (2) two opus verdicts conflict and the task can't proceed without arbitration. Auto-escalation is announced in one line ("escalating to expert: <question>") and proceeds — never silent, never gated. On a harness with no Fable tier (see the mapping), escalation caps at Opus: stop and report to Leo instead, offering to continue at the Opus tier or hand off to a harness that has the expert rung.

## Execute means execute-then-review

Every implementation request — "fix", "implement", "execute the plan", anything that changes code — implicitly includes a review phase, whether or not review was mentioned. Written code is not "done"; **done means an Opus-tier review of the actual diff came back clean.**

1. Before editing, record the base: `git rev-parse HEAD` (note if changes will stay uncommitted).
2. Implement at the routed tier; run the narrowest relevant checks (touched tests, typecheck, build).
3. Have the `reviewer` role judge the actual diff, passing the base ref (or "uncommitted working tree") and the original request/plan text. Never self-review instead. Review runs at the Opus tier by default. Downscale to a Sonnet-tier review ONLY for a clearly-trivial diff — ALL of: ≤ 2 files, ≤ ~60 changed lines, mechanical/boilerplate class (rename, format, comment, constant/string tweak, dependency-version bump, test-data edit), and no risky-path match (auth, payments/billing, crypto/secrets, DB migration or schema, CI/CD config, access control). If any condition fails or you are unsure, keep the full Opus-tier review — the default bucket is today's behavior. Never skip review because the change "is small". Only exemptions (no review at all): docs/comment-only diffs, and edits Leo dictated verbatim.
4. Blocking findings: fix at the executing tier, re-review the fix only. ONE cycle — if the second review still blocks, stop and report the findings to Leo instead of looping, offering `expert` arbitration as one of the options (where the Fable rung exists).
5. Report done as three lines: what changed / checks run / review verdict.

## Delegate the labor

The main loop orchestrates; delegated roles do the volume. In an expensive-tier session, inline bulk work burns the expensive tier — delegate down:

- Locating code, mapping structure → `Explore` (Haiku tier), in parallel when questions are independent.
- Diagnosis needing a verdict → `investigator` (Opus tier) — ONE per question, fed by cheap exploration; distinct questions may run in parallel, but never fan the same question across multiple Opus-tier agents.
- Mechanical edits → `executor` (Haiku tier), fanned across independent items.
- Executing a written plan → `implementer` (Sonnet tier).
- Judging a diff → `reviewer` (Opus tier).
- Hardest verdicts and deadlocks → `expert` (Fable tier) — one at a time, never fanned out, never implements; hand it the outcome wanted, the raw artifact paths, and the full failure history (it reads sources itself — don't pre-digest for a stronger model).

Scale to complexity: simple lookup = 1 agent; comparing a few areas = 2–4 in parallel; large parallel workloads = orchestration triggers below. Multi-agent costs ~15× a single chat — reserve fan-out for genuinely parallel, high-value work. In an expensive-tier session this is a hard rule, not a heuristic: implementation and mechanical edits MUST go to `implementer`/`executor`, and code searches to `Explore`; editing or grepping inline is the exception, reserved for a trivial single-file touch (< ~10 lines) where writing the spec would cost more than the change. More than ~3 inline file edits or ~5 inline searches in an expensive-tier session means the work should have been delegated. Dispatch mechanics — brief structure, the return-status contract, durable progress — live in leo:delegation.

## Orchestration triggers

These phrases are my standing opt-in to multi-agent orchestration: **"fan this out"**, **"workflow this"**, **"grind on this"**, **"do this properly"**.

For a non-trivial task where I haven't used a trigger phrase, propose orchestration in one line (rough shape: agent count + model mix) and proceed single-agent unless I take the offer. Never launch a large fan-out silently. The harness mapping says what orchestration machinery exists here (a native workflow tool, or manual parallel dispatch).

## Machine-local state

Any skill or agent that needs to persist information writes JSON to `$LEOS_AGENT_PATH/local/<skill-or-agent-name>.json` — `LEOS_AGENT_PATH` is an optional override, unset it defaults to `~/.leos-agent` (in bash: `${LEOS_AGENT_PATH:-$HOME/.leos-agent}`). Top-level keys are `owner/repo` (or the absolute project path when there's no GitHub repo): **data always stays separate per repo/project**. Read and write through `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/state.py"` (`get` / `merge` / `path`) instead of hand-rolling read-modify-write — the code ships with the plugin, the data stays under `${LEOS_AGENT_PATH:-$HOME/.leos-agent}/local/`, gitignored, per-machine, never synced, and survives plugin updates. Examples: `review-watcher.json` (PR numbers already auto-reviewed), `resolve-ticket.json` (ticket-prefix → tracker mappings).

## Cost discipline

Spend expensive tokens on planning, verification, and synthesis (low volume, high leverage); spend cheap tokens on execution volume. When dispatching delegated work, pin the tier per task — executors default to the Sonnet tier at low effort, mechanical work to the Haiku tier, judges/verifiers to the Opus tier. The Fable tier is the most expensive per call and cheap as a policy only because it fires rarely and only on verdicts — batch fan-outs never auto-use it (that is exactly where a Fable jump silently multiplies cost).

## Skill index

Reach for the matching skill at the decision point — each one encodes the mechanics these directives already assume, sized to the work, not extra ritual.

| At this point | Consult |
|---|---|
| A bug or failing test, before any fix | leo:debugging |
| An approach not yet settled, before non-trivial code | leo:brainstorming |
| Turning a chosen approach into a plan | leo:writing-plans |
| Carrying out a written plan | leo:executing-plans |
| Adding or changing runtime behavior | leo:test-first |
| Before claiming anything done / fixed / passing | leo:verification |
| Dispatching subagents or a fan-out | leo:delegation |
| Isolating branch work | leo:worktrees |
| Landing or cleaning up a finished branch | leo:finishing-a-branch |
