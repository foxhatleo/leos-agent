---
name: memory
description: >
  Durable cross-harness facts, one per file, in a store that outlives the
  session and every plugin update. Covers what earns a place in the store,
  how a fact is written and revised, how to read one before acting on it,
  and when to throw one away. The store is canonical; each harness's own
  memory surface receives a generated copy of the global facts.
when_to_use: >
  A fact surfaces that will still be true next month — a stated preference,
  a repo rule the code does not spell out, a settled decision, a machine
  quirk that cost you a detour. Also when a remembered fact turns out wrong
  and has to be revised or dropped. NOT for anything scoped to the current
  task (branch names, what is failing right now — that is machine-local
  JSON state), and NOT for material the repository already records.
---

# memory

One fact per file, written the moment it is learned. A fact you intend to
record at the end of the session is a fact you will lose, because the end of
the session is exactly where context runs out.

The store is the only place you write. Each harness's native memory file
receives a generated copy of the global facts, so a preference learned on one
harness is in front of you on the next one. Those copies are derived — editing
one changes nothing and is overwritten on the next write.

## What earns a place

All three must hold. Miss one and it is not a memory.

1. **It is durable.** Still true a month from now. Not the branch you are on,
   not the test that is failing, not where you are in the current task.
2. **It is not cheaply re-derivable.** You could not recover it from one grep
   or one file read in the repo you are already sitting in.
3. **It fits one of the five types below.** There is no sixth type, and that
   closed set is the whole gate.

## The five types

1. **preference** — Leo said how he wants something done, and it outlives this
   task. *"Squash-merge, never a merge commit."*
2. **convention** — a rule of this repo the code does not state, usually
   learned the hard way. *"The adapters directory is generated; hand edits are
   swept on the next render."*
3. **environment** — a machine or tooling fact that cost a detour to establish.
   Never a credential.
4. **decision** — a settled choice and its one-line reason, where reopening it
   would cost a conversation.
5. **person** — who owns or decides what, and how to reach them about it.

## When it doesn't — Exemptions

A closed, named list. Outside it the default holds — no free pass by analogy.

1. **Task state** — anything true only until this task ends. Branch names, PR
   numbers, what you are about to do next. That belongs in machine-local JSON
   via `${CLAUDE_PLUGIN_ROOT}/scripts/state.py`, not here. (`${CLAUDE_PLUGIN_ROOT}`
   is the Claude Code spelling of the plugin root and is not substituted into
   this text; leo:delegation's ledger section gives the per-harness forms.)
2. **Re-readable facts** — anything one search away in the working tree. The
   repository is not something to memorize.
3. **Your own conclusions** — an analysis, a diagnosis, a plan. A memory
   records what Leo or the world asserted, not your reasoning about it.
4. **Restatements of policy** — anything already in leo:using-leo or another
   leo skill. Two copies of one rule drift apart, and the copy wins by being
   nearer to hand.
5. **Secrets** — tokens, keys, passwords, private URLs. Never, under any type:
   the store is plain text on disk.
6. **One-off corrections** — Leo redirecting you inside this task. Only a
   correction he generalizes becomes a preference.

## Rate discipline

Automatic capture without a brake becomes a log, and nobody trusts a log.

- At most **three** unprompted writes in a session. Reaching for a fourth means
  you are recording activity, not learning facts — consolidate instead.
- Announce every write in one line: `remembered: <title> (preference)`. A store
  that grows invisibly is a store Leo cannot audit.
- Check the scope before writing. A fact that restates one already there is a
  revision of that file, never a second file beside it.

## Procedure

Write, with the body on standard input:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory.py" write global preference "Squash merge"
```

Repo-scoped facts take an explicit key — the working directory is never
guessed, because a worktree would attribute the fact to the wrong project:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory.py" write repo convention "Generated adapters" --repo owner/name
```

Writing the same title again revises that file in place and keeps its original
creation date. `list` shows what is stored; `read <ref>` returns one fact whole.

## Read path

1. The index arrives in context on every harness whose mapping says so. Each
   line is a pointer, not the fact — the one-line hook is lossy by design.
2. Read the file before you rely on it.
3. **What you can see beats what you remember.** When a stored fact disagrees
   with the repository in front of you, the repository is right. Use the
   observation, then revise the memory. Never act on a fact you just watched
   fail.

## Forget path

Three triggers, and no others: Leo says it is wrong or has changed; you
observed it to be false; or its subject no longer exists.

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory.py" forget global/squash-merge
```

Forgetting moves the file aside rather than destroying it, so a wrong call is
recoverable. A superseded fact is a revision, not a forget followed by a write.
Suspicion that something looks stale is not grounds to drop it — that needs an
assertion or an observation.

## Self-talk to catch

- "I'll write this down once the task settles" — the task ending is what takes
  the fact with it.
- "This is worth keeping, roughly" — name its type, or it does not go in.
- "The memory says the flag is called that" — the memory says what was true
  when someone wrote it; check the flag.
- "Leo corrected me, that's a preference" — inside one task it is a
  correction; only a generalization is a preference.

## Works with

- leo:using-leo — draws the line this skill sits on: per-task JSON state on one
  side, durable facts on the other.
- leo:doctor — reports whether the store exists and whether each harness
  actually received its copy.
- leo:verification — a stored fact is not evidence. Claims still need a fresh
  command run this turn.
