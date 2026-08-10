---
name: delegation
description: >
  How to dispatch a subagent so its report is trustworthy: writing a brief
  that survives having no conversation history, pinning model and effort, the
  four states a report may resolve to, and keeping progress durable across a
  long run.
when_to_use: >
  Any time work goes to a subagent instead of being done inline, single
  dispatch or fan-out. Not for picking which tier the work belongs in, which
  is leo:routing, and not for judging what comes back, which is
  leo:review-gate.
---

# delegation

Core rule: a subagent gets one shot at the brief and no session history. If
the brief doesn't stand alone, the dispatch is already broken.

## Writing the brief

Every dispatch is self-contained: goal, constraints, exact file paths, the
checks to run, and what the return must contain. Write it as if for a
stranger who will never see this conversation — because that's what a
subagent is. A brief missing a file path or a check produces a report that
looks done and isn't.

Bad: "fix the flaky auth test." Good: "`tests/auth/session_test.py::test_expiry`
fails intermittently (repro: run it 20x, ~1 in 8 fails). Fix the race, keep
the test's intent unchanged, don't touch other tests in the file. Run
`pytest tests/auth/session_test.py -x` 20 times clean before reporting done.
Return: files touched, the race you found, the check output." The second
version needs no follow-up question; the first invites three.

## Pin model and effort

Where the harness lets a dispatch choose them, every spawn pins **model and
effort** from leo:routing — the judging tier for verdicts and diagnosis, the
middle tier for normal implementation, the cheap tier for mechanical work. An
unpinned call inherits the session's own tier: in an expensive session that
means every cheap spawn quietly runs at the expensive rate, and a ten-item
fan-out bills judge money for work that needed none. Pin both, not only the
ones that obviously need it.

Where the harness fixes them per registered role instead, that pin is already
made and the dispatch cannot override it — so the choice of *role* is the
choice of tier, and picking the wrong role is the whole mistake. Where it
offers neither, say so rather than reporting a tier you did not actually set.
Check your section of the harness reference under **Tier pinning** before
assuming which of the three applies.

## The four-state return contract

A subagent's report must resolve to exactly one of four states. Don't accept
a report that hedges across two of them.

| State | Means | Your response |
|---|---|---|
| `done` | Work finished, matches the brief | Verify against artifacts — see leo:verification — never take the self-report at face value |
| `concerns` | Finished, but flags something worth a second look | Read the concerns before accepting; they're often the real finding |
| `needs-context` | Blocked on missing information you can supply | Send the missing piece to the same agent (`SendMessage` on Claude Code, `followup_task` on Codex — elsewhere see the *Follow-up to a live agent* row of your mapping, and where none is established, cold re-dispatch with the context restated is the whole mechanism) so it keeps the context it already built. Either way **once** — a second needs-context on the same gap means the brief itself is broken, escalate the tier |
| `blocked` | Blocked on something you can't hand over inline | Resolve the blocker, or escalate per the ladder — never a silent same-tier retry |

`needs-context` and `blocked` look similar; the test is whether the missing
piece is something *you* hold (needs-context — a file path, a decision, a
credential) or something neither of you can supply without more work
(blocked — a failing external service, a genuinely ambiguous requirement).

Each role's own prompt carries the state line it must emit, so the contract
is enforced at both ends. One role is deliberately narrowed: a reviewer emits
only `done` / `needs-context`, because severity already lives in
`blocking`/`non-blocking` and the diff's own answer in
`approved`/`needs-changes`. `status` is a separate axis from `confidence`:
status routes your next move, confidence rates the work.

## Long multi-agent runs: durable progress

A run spanning many dispatches survives context compaction only if progress
is persisted outside the conversation. Each entry needs an item id, a status
(one of the four above, plus `pending` / `in-progress`), and an artifact path
— a branch name, a file, a diff. On resume, read that record first: anything
already `done` or `concerns` is not re-dispatched, and anything `blocked` is
reported rather than quietly retried.

Where the harness owns a task list of its own, that list *is* the record.
Use it, and do not build a second one alongside it — two ledgers disagreeing
about which item finished is worse than either alone.

Everywhere else, use `<plugin-root>/scripts/state.py` (`get` / `merge` /
`path` — flock-guarded, atomic writes, keyed per repo) rather than notes in
the transcript. An entry is small:

```
python3 "<plugin-root>/scripts/state.py" merge <skill-name> <owner/repo> \
  '{"items": {"<id>": {"status": "done", "artifact": "branch:fix/eng-123-slug"}}}'
```

Small, but it is the only thing between a compaction mid-run and forty items
re-dispatched from the first. Write it as each dispatch resolves, never
batched at the end: the gap between "agent finished" and "record written" is
exactly what this closes.

`<plugin-root>` is spelled differently per harness — `${CLAUDE_PLUGIN_ROOT}`
on Claude Code, `$PLUGIN_ROOT` on Codex, `$CURSOR_PLUGIN_ROOT` on Cursor. On
OpenCode no such variable exists; use the absolute path of the installed
package. Never invent an environment variable the harness does not export.

Check the **Durable progress** row of your harness section before choosing
between the two mechanisms.

For a batch of independent, well-scoped fixes, don't hand-roll the loop where
a runner exists: `<plugin-root>/workflows/cost-tiered-fix.js` already
implements plan → tiered execute → verify with bounded escalation and its own
progress tracking. The **Workflow runner** row says whether this harness can
execute it. Where it cannot, the record above is the whole mechanism.

## Parallel dispatch: own your files

Fan-out is safe only when each spawn writes to **disjoint** files — no two
concurrent dispatches touching the same path. If the work can't be split
into disjoint file sets (one coherent change that happens to span many
files, like a single ticket fix), don't fan out — either run it sequentially
in one dispatch, or give each spawn its own isolated tree via leo:worktrees
so parallel edits can't collide even when the file sets overlap.

## Self-talk to catch

- "I'll skip pinning effort, model is enough" — no; an unpinned effort on an
  opus judge still runs at opus prices, at auto effort, which is not what
  the routing table costed out.
- "The brief is short, they'll infer the rest" — a subagent infers nothing;
  it has this brief and nothing else.
- "It said needs-context, I'll just re-ask the same way" — re-dispatching
  with the identical brief reproduces the identical gap; either add the
  missing piece or step up a tier. And prefer messaging the same agent over
  a fresh spawn: a cold re-dispatch pays again for the context it already
  built and can rediscover the same gap from a different angle.
- "Two spawns editing the same file will probably be fine, they touch
  different functions" — same file is not disjoint; sequence them or
  isolate with a worktree.
- "This ten-item fan-out is basically cost-tiered-fix, I'll just write the
  loop myself" — the workflow already handles escalation and orphan
  tracking; reinventing it inline drops that for no reason.

## Works with

- leo:verification — how a `done` report gets checked against real
  artifacts, not trusted as stated.
- leo:worktrees — file isolation for parallel dispatches that can't be made
  disjoint by scope alone.
