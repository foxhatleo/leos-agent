---
name: review-pr
description: Review a GitHub pull request of this repository and stage inline comments as a PENDING review only Leo can see. Never submits, and never reviews the local working diff. Requires gh, authenticated.
argument-hint: "[pr-number]"
---

# /review-pr — stage a pending GitHub review

This is the **dispatch contract** for the leos-agent review. The procedure it
dispatches lives in two files the main thread never reads:

| File | Read by |
|---|---|
| `skills/review-pr/reference/procedure.md` | the reviewer subagent |
| `skills/review-pr/reference/lenses.md` | the lens sub-subagents |

**The whole review runs inside one subagent.** A review is exactly the shape the
main thread must not absorb — a full diff, a ticket, N lens reports, and the
discarded candidates — for a durable output of one verdict and one table. So the
main thread reads no diff, no ticket, and no review thread. It dispatches, waits,
and relays.

Two tiers, three levels:

| Level | Who | Tier |
|---|---|---|
| Main thread | dispatches, relays | — |
| **Reviewer** subagent | the whole procedure; judges; owns every mutation | **standard** (inherit) |
| **Lens** sub-subagents | the fan-out; read and report only | **leo-runner** (`subagent_type: "leo-runner"` on Claude Code; the installed profile on Codex) |

Where the harness has no `leo-runner` agent and no per-spawn model override,
agents run at whatever they are registered with — say so in the report.

## Dispatch — the main thread's entire job

1. Resolve the plugin root to an **absolute path** — the directory holding
   `rules/preferences.md`, from `$LEOS_AGENT_ROOT`, `$CLAUDE_PLUGIN_ROOT`, or
   `$PLUGIN_ROOT`. A brief that repeats an unexpanded placeholder hands the
   reviewer a path that expands to nothing.

2. Spawn **one** reviewer subagent at the **standard** tier with a clean
   conversation context. On Codex pass `fork_turns="none"`; on another harness
   use its fresh-child equivalent when available. Give it:

   - the PR number, or "the current branch's PR" when Leo passed none
   - any focus hints Leo passed
   - the absolute plugin root
   - an instruction to read
     `<plugin-root>/skills/review-pr/reference/procedure.md` and follow it —
     by path, so it reads the steps itself rather than receiving them
     paraphrased
   - that it may fan out to lens sub-subagents, and that its final message must
     be the procedure's Step 6 report and nothing else

3. Wait. Do not poll it, do not run any `gh` or `git` command yourself, and do
   not pre-fetch the diff "to help" — that reintroduces exactly the context this
   dispatch exists to keep out.

4. Relay the returned report to Leo substantially intact — the tables, the
   coverage line, the verdict, the closing sentence. Compress prose if you must;
   never re-summarise a verdict into a different one, and never restate a staged
   comment in your own words. If the reviewer returned something that is not a
   Step 6 report, say so and report the failure rather than reconstructing a
   review from its fragments.

If this harness cannot spawn a subagent at all, read `reference/procedure.md`
and run it in the main thread, and open the report by saying the review was not
isolated. That is a degraded run, not the design.
