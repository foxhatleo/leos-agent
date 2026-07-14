# Leo's global Claude directives

These apply in every session on every machine. Canonical copy: `~/.leos-agent/claude/CLAUDE.md`.

## Model routing

Tier every task by the kind of work, not per session. When a request spans phases ("investigate X and fix it"), split it and tier each phase separately.

| Work type | Typical verbs | Tier | Do it via |
|---|---|---|---|
| Investigation | investigate, diagnose, debug, root-cause, "why does…" | Opus | `investigator` subagent |
| Planning / design | plan, design, architect, decide | Opus | plan mode in the main loop |
| Implementation | implement, fix, build, refactor, execute | Sonnet | main loop if session is Sonnet, else `implementer` |
| Mechanical | rename, codemod, apply known pattern, boilerplate, format | Haiku | `executor` subagent |
| Review / verification | review, verify, audit, judge | Opus | `reviewer` subagent on the real diff |
| Hardest problems / arbitration | "use expert", "deep thinking", "deep investigate", Fable by name | Fable | `expert` subagent |

**Escalate, don't struggle**: if a cheap-tier task turns out ambiguous or fails twice, step up one tier rather than retrying at the same tier. When the right tier is unclear, default up — **capped at Opus**. The Fable rung is never a default and never resolves tiering doubt; it is reached only by my trigger phrases above, or automatically in exactly two situations: (1) an opus-tier agent failed twice or reported low confidence on the same question, (2) two opus verdicts conflict and the task can't proceed without arbitration. Auto-escalation is announced in one line ("escalating to expert: <question>") and proceeds — never silent, never gated.

## Execute means execute-then-review

Every implementation request — "fix", "implement", "execute the plan", anything that changes code — implicitly includes a review phase, whether or not review was mentioned. Written code is not "done"; **done means an Opus review of the actual diff came back clean.**

1. Before editing, record the base: `git rev-parse HEAD` (note if changes will stay uncommitted).
2. Implement at the routed tier; run the narrowest relevant checks (touched tests, typecheck, build).
3. Spawn `reviewer` on the actual diff, passing the base ref (or "uncommitted working tree") and the original request/plan text. Never self-review instead; never skip because the change "is small". Only exemptions: docs/comment-only diffs, and edits Leo dictated verbatim.
4. Blocking findings: fix at the executing tier, re-review the fix only. ONE cycle — if the second review still blocks, stop and report the findings to Leo instead of looping, offering `expert` arbitration as one of the options.
5. Report done as three lines: what changed / checks run / review verdict.

## Delegate the labor

The main loop orchestrates; subagents do the volume. In an Opus session, inline bulk work burns the expensive tier — delegate down:

- Locating code, mapping structure → `Explore` (Haiku), in parallel when questions are independent.
- Diagnosis needing a verdict → `investigator` (Opus) — spawn ONE per question, fed by cheap exploration; distinct questions may run in parallel, but never fan the same question across multiple Opus agents.
- Mechanical edits → `executor` (Haiku), fanned across independent items.
- Executing a written plan → `implementer` (Sonnet).
- Judging a diff → `reviewer` (Opus).
- Hardest verdicts and deadlocks → `expert` (Fable) — one at a time, never fanned out, never implements; hand it the outcome wanted, the raw artifact paths, and the full failure history (it reads sources itself — don't pre-digest for a stronger model).

Scale to complexity: simple lookup = 1 agent; comparing a few areas = 2–4 in parallel; large parallel workloads = orchestration triggers below. Multi-agent costs ~15× a single chat — reserve fan-out for genuinely parallel, high-value work. Smell test: more than ~3 inline file edits or ~5 inline searches in an Opus session means I should have delegated.

## Orchestration triggers

These phrases are my standing opt-in to multi-agent orchestration (Workflow tool): **"fan this out"**, **"workflow this"**, **"grind on this"**, **"do this properly"**.

For a non-trivial task where I haven't used a trigger phrase, propose orchestration in one line (rough shape: agent count + model mix) and proceed single-agent unless I take the offer. Never launch a large fan-out silently.

## Machine-local state

Any skill or agent that needs to persist information writes JSON to `$LEOS_AGENT_PATH/local/<skill-or-agent-name>.json` — `LEOS_AGENT_PATH` is an optional override, unset it defaults to `~/.leos-agent` (in bash: `${LEOS_AGENT_PATH:-$HOME/.leos-agent}`). Top-level keys are `owner/repo` (or the absolute project path when there's no GitHub repo): **data always stays separate per repo/project**. Read and write through `$LEOS_AGENT_PATH/claude/scripts/state.py` (`get` / `merge` / `path`) instead of hand-rolling read-modify-write. `local/` is gitignored — this state is per-machine, never committed, never synced. Examples: `review-watcher.json` (PR numbers already auto-reviewed), `resolve-ticket.json` (ticket-prefix → tracker mappings).

## Cost discipline

Spend expensive tokens on planning, verification, and synthesis (low volume, high leverage); spend cheap tokens on execution volume. In workflow scripts, set `model` and `effort` per `agent()` call — executors default to sonnet + effort low, mechanical work to haiku, judges/verifiers to opus. Fable (`expert`) is the most expensive tier per call and cheap as a policy only because it fires rarely and only on verdicts — workflow scripts never auto-use it (batch fan-out is exactly where a fable jump silently multiplies cost). Reusable workflow scripts live in `~/.leos-agent/claude/workflows/`.
