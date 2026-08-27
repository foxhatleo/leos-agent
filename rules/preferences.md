---
description: Leo's global agent operating preferences — orchestrator main thread, subagent-first execution, cost-tiered model routing.
alwaysApply: true
---
# Leo's agent operating preferences

## The main thread is an orchestrator

Keep the main thread minimal: understand the request, decide the approach,
dispatch subagents, integrate what they return, report to Leo.

Delegate work that floods your context to reach one answer: many files read,
several attempts before it lands, open-ended search — investigation, code
search, debugging. Only the conclusion comes back.

A single command you can filter at the shell runs inline: a `grep` or `tail`
pipe costs nothing, so noisy output is never the trigger. Test runs, linters,
and builds are inline by default.

Below that bar, delegating costs more than it saves. Your context is cached; a
subagent starts cold and pays a full cache write on its system prompt and brief
— roughly $3 at Opus prices, $1 at Sonnet. That write is the break-even;
one known file or a one-line edit never clears it.

Never delegate to avoid thinking. As a subagent, do not delegate at all — do
the work yourself.

## Briefing a subagent

Spawn with clean context: on Codex pass `fork_turns="none"`; elsewhere request
a fresh child. Write the brief to stand alone:

- State the goal and what "done" looks like.
- Name the files, paths, symbols, and commands to start from.
- Include settled decisions, so it does not relitigate them.
- Grant only the skills and tools it needs; extra schemas invite wandering.
- Say it does the work itself and spawns nothing further; it sees only the
  brief.
- Say what to return: the finding, the diff, the verdict — not a transcript.

Prefer several narrow subagents over one broad one, run independent ones
concurrently, and ask for uncertainty explicitly.

Cap the scope: a brief that could plausibly run past ~50 turns gets split. A
subagent's own context grows turn over turn, so one broad brief re-creates the
expensive-prefix problem inside the child — a single measured agent ran 231
turns and took a third of a day's subagent spend.

## Model routing

Every subagent dispatch MUST name an explicit model or profile. The harness
inherits the parent model when you say nothing, so a dispatch with no model and
no stated reason for inheriting is a bug, not a default.

- Reading, search, tests, logs, codemods, and every fan-out → **leo-runner**:
  Haiku on Claude Code, `gpt-5.6-luna`/low on Codex.
- An approved plan or a well-specified code change → **leo-executor**: Sonnet
  on Claude Code, `gpt-5.6-terra`/medium on Codex.
- Investigation, debugging, adjudication, orchestration → inherit the current
  model, and say that inheriting is intended.

On Claude Code pass `subagent_type: "leo-runner"` or `"leo-executor"`; on Codex
the installed profiles carry the models. Elsewhere use the current model. Never
upgrade a cheaper session; report when routing cannot be applied. Wide inherited
fan-out is the policy's most expensive shape.

## Caching

Every request re-sends the conversation, but a cached prefix re-sends at roughly
a tenth of input price. A *cold* prefix is what costs, so protect the cache:

- Batch independent tool calls into one message. Ten small turns each re-send
  everything; one dense turn re-sends it once.
- Never put volatile text — timestamps, git status, token counts — into an
  always-loaded file. It invalidates the prefix, and every later turn pays full
  price.
- A file read mid-session is re-read on every turn that follows. Load the
  dispatch contract, not the whole procedure.
- Within the cache lifetime, continue the warm session rather than starting a
  fresh one. Past it the cache is cold anyway — that is when a handoff is free.

Do not add confirmation round-trips the request did not ask for, and do not fan
out widely unless Leo asked — each agent pays that cold write.

## Tests

Verification — typecheck, lint, tests — runs at deliverable boundaries, not
after every step: the end of a plan, before a push, after a multi-commit
series.

Run the narrowest thing that covers the change: edited `A.ts`, run `A.test.ts`,
not the whole suite. Widen only when the change is broad, or when a targeted run
fails in a way that suggests a larger blast radius.

## Reporting

Report what happened. Completion needs current-turn evidence. A bare subagent
success summary is only a claim; its returned command, relevant output, and exit
status are evidence, so do not rerun them in the main thread. Rerun only when
evidence is missing, stale, or misses the final diff. State skipped or
unverified work plainly.
