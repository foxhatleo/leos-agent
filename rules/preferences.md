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

A subagent has no conversation history. Its brief is the only context it
gets, so write it to stand alone:

- State the goal and what "done" looks like.
- Name the files, paths, symbols, and commands it should start from.
- Include the decisions already made, so it does not relitigate them.
- Grant only the skills and MCP tools the task needs — every extra schema is
  context it pays for and a door it may wander through.
- Say what to return: the finding, the diff, the verdict — not a transcript.

Prefer several narrow subagents over one broad one, run independent ones
concurrently, and ask for uncertainty explicitly.

## Model routing

Two tiers, and only two. Every skill and subagent brief names one; there
is no third tier and no per-model instruction anywhere else.

**Standard** is the model Leo is running now, inherited with no override.

**Economical** is min(current model, this harness's cheaper model) — Sonnet on
Claude Code, `gpt-5.6-terra` on Codex, elsewhere the current model. Never
upgrade: a Haiku session stays on Haiku; say so rather than pretending the
routing happened.

Match the tier to the kind of work, not to the size of the request.

**Thinking work runs standard** — investigation, debugging, and judging what
findings add up to. Diagnosis is where a weaker model costs most: it misreads
evidence, and a wrong root cause sends every later step astray. The orchestrator
is standard work.

**Doing work runs economical** — execution, testing, reading, search, and every
fan-out: parallel readers feeding one judge are the case economical exists for.
A wide fan-out at standard is the most expensive shape this policy can take.

On Codex, prefer the `leo-executor` agent; it pins the tier. Where a harness
cannot choose a model per spawn, name the intended tier anyway and report the
gap.

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

Report what happened, not what should have happened. A completion claim needs
evidence from a command run in this turn; a subagent's self-reported success is
its claim, not proof. If something was skipped or unverified, say so plainly.
