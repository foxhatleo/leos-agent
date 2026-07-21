---
name: worktrees
description: >
  Worktree lifecycle mechanics for isolated branch work — detect, create,
  and clean up a git worktree so implementation happens off the main
  checkout. Shared by resolve-ticket, executing-plans, and delegation
  fan-outs; not itself a workflow, just the plumbing they all call into.
when_to_use: >
  Any skill or agent about to create or tear down a worktree for isolated
  branch work. NOT for choosing whether isolation is needed in the first
  place (that call belongs to the calling skill's plan/gate step) and NOT
  for merging or cleaning up a finished branch's remnants after the PR
  lands — that's leo:finishing-a-branch.
---

# Worktrees

Core rule: **detect existing isolation before creating anything, and never
remove a worktree from inside it.**

## When this fires

A calling skill has already decided it wants isolated branch work (a plan
was approved, a fan-out item needs its own tree) and needs the mechanics:
enter, verify, exit, clean up. This skill doesn't decide *whether* to
isolate — that's upstream. It also doesn't cover post-merge branch
deletion or remote cleanup; once the PR lands, hand off to
leo:finishing-a-branch.

## Procedure

### 1. Detect existing isolation first

Before creating anything, check whether the session is already inside a
worktree:

```
git rev-parse --git-common-dir
git rev-parse --git-dir
```

If they differ, the current checkout **is already a worktree** — the
session's own isolation. Never nest a worktree inside a worktree: do the
work here, or exit to the main checkout first if a *different* branch
needs its own tree. Nesting produces a git state no cleanup step can
untangle cleanly.

### 2. Prefer the native tools

`EnterWorktree` / `ExitWorktree` are harness-managed: they track which
worktree belongs to which session and auto-clean on exit. Default to them.

### 3. Raw-git fallback, only when the native tools are unavailable

Fixed location convention: `.claude/worktrees/<name>`. Before creating,
verify the location is actually ignored:

```
git check-ignore .claude/worktrees/<name>
```

No output (or a non-zero exit) means it isn't ignored — stop and fix
`.gitignore` first. A worktree directory that git tracks will fight every
subsequent commit in the main checkout. Only after `check-ignore` confirms
it, run:

```
git worktree add -b <branch> .claude/worktrees/<name> <base-ref>
```

### 4. Cleanup is provenance-gated

Before removing any worktree, establish who created it:

- Path under `.claude/worktrees/<name>` (the convention dir) **and** this
  system created it → safe to remove.
- Created via `EnterWorktree` → belongs to its `ExitWorktree`, not to raw
  `git worktree remove`. Use the matching exit tool; don't hand-remove a
  harness-tracked worktree, it loses the session-tracking state.
- Anything else — a path outside the convention dir, or one this system
  didn't create — is the user's. Leave it alone; report it, don't touch it.

Provenance is the only gate. A worktree existing and looking abandoned is
not permission to remove it; confirm it's one this system made via the
convention path (or the matching Enter/Exit pairing) before it goes.

### 5. Never remove a worktree from inside it

`cd` to the main checkout first — removing a worktree while it's the
current working directory leaves git in a state that needs manual repair.
Sequence:

```
cd <main-checkout>
git worktree remove .claude/worktrees/<name>
git worktree prune
```

`ExitWorktree` handles this ordering itself when used; the manual sequence
above is only for the raw-git fallback path.

## Live config repo caveat

In Leo's own setup, files under this repo (`~/.leos-agent`) may be wired
into the running environment via symlinks or hooks — editing them in place
can break the very session doing the editing. Restructuring work on this
repo happens in a worktree so the live tree stays intact while the change
is built and reviewed. This skill's own file was written that way: this
migration is the example, not a hypothetical.

## Self-talk to catch

- "It's probably fine to reuse the current checkout" — check
  `--git-common-dir` vs `--git-dir` first; don't guess from vibes.
- "This worktree looks stale, I'll just remove it" — stale isn't
  provenance. Confirm the convention path or the Enter/Exit pairing.
- "I'm already in the worktree, `git worktree remove .` should work" —
  never remove a worktree from inside it; cd out first.
- "Skipping check-ignore, the convention dir is obviously gitignored" —
  verify it every time; a missing `.gitignore` entry silently breaks the
  main checkout's commits.

## Works with

- resolve-ticket — Step 5 (Worktree) calls this for enter, Step 8 (Ship)
  calls this for exit.
- executing-plans — isolates plan execution the same way.
- leo:finishing-a-branch — post-merge cleanup once the PR lands; out of
  scope here.
