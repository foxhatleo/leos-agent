---
name: freshness
description: >
  Currency gate for code written against anything outside this repository.
  Before committing to a library call, CLI flag, endpoint field, or vendor
  number, confirm its shape against the version this project actually runs —
  the installed package, the lockfile pin, or documentation fetched this turn.
  Each check is reported by symbol and source.
when_to_use: >
  About to write, review, or assert the shape of a third-party surface. Not
  for first-party code in this workspace, not for a pinned runtime's standard
  library, and not a substitute for running anything — leo:verification owns
  the completion claim.
---

# freshness

A third-party surface you have not read this session is a guess, however
familiar it feels. Recall of a library is a snapshot of some arbitrary past
version; it is not a snapshot of the one pinned in this lockfile. The cost is
a call that reads perfectly and does not exist.

Recall is not a source. The package installed on disk is.

## When it fires

A closed list of five.

1. **A symbol you did not read this session** — a function, method, class,
   decorator, flag, or config key belonging to something not defined in this
   working tree.
2. **A version-sensitive call shape** — argument order, keyword names, return
   type, or import path for a dependency whose installed version you have not
   confirmed.
3. **A service contract** — endpoint path, request or response field, auth
   scheme, pagination rule, error code.
4. **A vendor-schedule fact** — a model id, context window, price, rate limit,
   or regional availability. These move on someone else's calendar.
5. **A deprecation or removal claim** — "that was dropped in v3" is an
   assertion about a moving target and needs the same check as a signature.

Outside these five, write the code.

## What counts as a source

Two different questions — which to reach for, and which one wins.

**Lookup order.** Cheapest first; stop at the first that answers.

1. A documentation tool the harness exposes for that vendor (Context7 and
   the like) — one call, cheap.
2. Official documentation fetched this turn — cheap.
3. The lockfile pin plus that version's changelog — a narrow read.
4. The installed package read on disk — `node_modules`, `site-packages`,
   `vendor` — expensive; grep for the specific symbol, never read whole
   files.

Rungs 1 and 2 answer for whichever version they happen to describe, which is
not always yours. Note the version each one reports and compare it to the pin;
a cheap answer that cannot say which version it describes has not answered.

**Authority.** When two sources disagree, the installed package wins — it
is the version that will execute. A cheap source that contradicts it is
wrong.

Not sources: your recollection; an older file in this repo calling the same API,
which may be the stale thing you are about to copy; a blog post; a search
snippet you did not open.

## When it doesn't — Exemptions

A closed, named list. Outside it the default holds — no free pass by analogy.

1. **First-party code** — defined in this repo or a sibling package in the same
   workspace. Read it; a fetch would answer a question the tree already answers.
2. **Standard library at a pinned runtime** — those shapes do not move between
   two runs of the same interpreter.
3. **Already checked this session** — one check per symbol. Cite the earlier
   check rather than repeating it.
4. **Covered by a red-to-green run against the real dependency** — a
   leo:test-first cycle that exercises the actual library is this check, and its
   transition is the record. Do not manufacture weaker evidence beside it.
5. **No fetch capability in this session** — offline, or no docs tool reachable.
   Then the claim is reported as unchecked and this exemption is named.

A skip must name its exemption in the report — "skipped freshness: first-party,
read src/auth/session.ts". An unnamed skip is an unchecked claim.

## Recording the check

One line per check, in the done report:

```
checked <symbol> against <source> (<version>)
```

The version in parentheses is what makes it auditable — a reviewer compares it
to the lockfile without rerunning anything.

## Self-talk to catch

- "I've used this library for years" — across how many major versions, and
  which one is pinned here?
- "The docs will only confirm what I know" — then it costs nothing, and the
  case where they do not is the entire reason for the step.
- "Another file here calls it this way" — that file may be what you are about
  to propagate.
- "The typechecker will catch it" — a typechecker reads installed stubs, the
  authority source. Say so and cite it, rather than skipping and hoping.
- "It's one argument" — argument names are exactly what moves between majors.

## Reviewable finding

An unchecked third-party surface with no named exemption is a finding:
blocking when the call sits on the path the task was about, non-blocking
otherwise.

## Works with

- leo:verification — that gate proves the code you wrote runs; this one governs
  whether the API you wrote it against exists. A green test against a mocked
  dependency satisfies that skill and not this one.
- leo:test-first — exemption 4; a red-to-green run against the real dependency
  has already done this work.
- leo:debugging — when Localize follows a path into a dependency, this says
  which copy of it to read.
