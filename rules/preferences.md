---
description: Leo's global agent operating preferences — orchestrator main thread, subagent-first execution, cost-tiered model routing.
alwaysApply: true
---
# Leo's agent operating preferences

## The main thread is an orchestrator

Keep the main thread minimal: understand the request, decide the approach,
dispatch subagents, integrate what they return, report to Leo. Bulk work belongs
in subagents.

Delegate work whose byproducts you do not want to keep: many files read, long
output, several attempts before it lands — investigation, code search,
debugging, execution, test runs. Only the conclusion comes back; the rest dies
with it.

Keep it inline when delegating costs more than it saves. Your context is already
cached; a subagent starts cold and pays a full cache write on its system prompt
and brief before reading anything. That write, not a screenful of output, is the
break-even — one known file or a one-line edit never clears it.

Do not spawn a subagent to avoid thinking. If you are a subagent, this section
does not apply — do the work yourself.

## Briefing a subagent

Spawn with clean context: on Codex pass `fork_turns="none"`; elsewhere request
a fresh child where supported. Report the gap if history inheritance cannot be
prevented. Write the brief to stand alone:

- State the goal and what "done" looks like.
- Name the files, paths, symbols, and commands it should start from.
- Include settled decisions, so it does not relitigate them.
- Where supported, grant only the skills and tools it needs; extra schemas cost
  context and invite wandering.
- Say what to return: the finding, the diff, the verdict — not a transcript.

Prefer several narrow subagents over one broad one, run independent ones
concurrently, and ask for uncertainty explicitly.

## Model routing

Every brief names one of two tiers; there is no third.

**Standard** is the model Leo is running now, inherited with no override.

**Economical** is min(current model, the cheapest sufficient profile): runner =
Haiku on Claude Code or `gpt-5.6-luna`/low on Codex; executor = Sonnet or
`gpt-5.6-terra`/medium. Elsewhere use the current model. Never upgrade a cheaper
session; report when routing cannot be applied.

Match the tier to the kind of work, not to the size of the request.

**Thinking work runs standard** — investigation, debugging, adjudication, and
orchestration. A weak diagnosis makes every later step wasteful.

**Doing work runs economical** — runner for tests, reading, search, logs,
codemods, and every fan-out; executor for an approved plan or well-specified
code change. On Codex these are `leo-runner` and `leo-executor`. Wide standard
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
