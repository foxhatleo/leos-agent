# Leo's global Claude directives

These apply in every session on every machine. Canonical copy: `~/.leos-agent/claude/CLAUDE.md`.

## Model routing

Pick the model tier per task from the kind of work, not per session. When a task spans phases ("investigate X and fix it and review"), split it and tier each phase separately.

| Work type | Typical verbs | Tier |
|---|---|---|
| Investigation | investigate, diagnose, debug, root-cause | Opus |
| Planning / design | plan, design, architect, decide | Opus |
| Implementation | implement, fix, build, refactor | Sonnet (default) |
| Mechanical | rename, codemod, apply known pattern, boilerplate, format | Haiku |
| Verification | review, verify, audit, judge | Opus |

- **Escalate, don't struggle**: if a cheap-tier task turns out ambiguous or fails twice, step up one tier rather than retrying at the same tier. When the right tier is unclear, default up.
- **Delegate the labor**: prefer spawning the `executor` subagent for well-specified mechanical work and the `investigator` subagent for read-only research, fanned out in parallel across independent items. Keep planning, synthesis, and verification in the main loop.

## Orchestration triggers

These phrases are my standing opt-in to multi-agent orchestration (Workflow tool): **"fan this out"**, **"workflow this"**, **"grind on this"**, **"do this properly"**.

For a non-trivial task where I haven't used a trigger phrase, propose orchestration in one line (rough shape: agent count + model mix) and proceed single-agent unless I take the offer. Never launch a large fan-out silently.

## Cost discipline

Spend expensive tokens on planning, verification, and synthesis (low volume, high leverage); spend cheap tokens on execution volume. In workflow scripts, set `model` and `effort` per `agent()` call — executors default to sonnet + effort low, mechanical work to haiku, judges/verifiers to opus. Reusable workflow scripts live in `~/.leos-agent/claude/workflows/`.
