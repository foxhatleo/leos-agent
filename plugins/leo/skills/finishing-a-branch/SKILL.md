---
name: finishing-a-branch
description: >
  End-of-branch state machine for once the work on a branch or worktree is
  done. Gates on a clean verdict, offers a closed set of dispositions — merge,
  pull request, keep, discard — and orders the teardown so the work lands
  before the worktree goes and the worktree goes before the branch.
when_to_use: >
  A branch has reached implementation-done and needs a disposition, or Leo
  says to wrap up or clean up this branch. Not for managing a worktree
  mid-task, which is leo:worktrees, and not a substitute for leo:review-gate.
---

# finishing-a-branch

Core rule: a branch doesn't get disposed of by momentum. It reaches one of
four terminal states, each chosen explicitly, and destructive ones require
saying out loud what gets lost.

## Precondition: review verdict, not vibes

Do not enter this skill's decision step without a clean **review verdict**
on the final diff. "Implementation looks done" is not a review verdict.

- If review hasn't run yet, or the last verdict was `needs-changes`: stop
  here, go run/finish the review cycle (see execute-then-review), come back.
- If review is `approved`: proceed.
- Never offer merge/PR on unreviewed or still-blocked work. "It's a small
  change" or "I already read through it" does not substitute for the
  reviewer's verdict — those are exactly the rationalizations this gate
  exists to block.

## The option set is closed

Once the gate passes, present exactly these four options — never an
open-ended "what would you like to do next?":

- **merge** — into the target branch, locally or via `gh pr merge`
- **PR** — open a pull request and stop (no local merge)
- **keep** — leave the branch/worktree exactly as-is, decide later
- **discard** — delete the branch and its worktree, work is gone

State the branch name, commit count ahead of the target, and the review
verdict when you present the set. Leo picks one; do not infer a choice from
silence, from a prior unrelated "yes," or from tone.

## Ordering (prevents self-referential failures)

Regardless of which path Leo picks, sequence matters — doing this out of
order breaks the tools that need the worktree or branch to still exist:

1. **cd out of the worktree first.** A shell sitting inside the worktree
   directory blocks its own removal.
2. **Merge (or push for a PR) BEFORE removing the worktree.** Land or
   publish the commits while the worktree still exists to operate from.
3. **Remove the worktree BEFORE deleting the branch.** Deleting the branch
   out from under a live worktree leaves the worktree metadata dangling and
   git in an inconsistent state.
4. Mechanics of steps 1–3 (which git worktree commands, how to prune) are
   owned by `leo:worktrees` — call into it rather than hand-rolling worktree
   surgery here. This skill decides *what* happens and *in what order*;
   `leo:worktrees` executes *how*.

Per option:

| Option | Sequence |
|---|---|
| merge | merge locally or `gh pr merge` → remove worktree (`leo:worktrees`) → delete local branch |
| PR | push branch → open PR → **stop** (worktree and branch stay; nothing is merged yet.) |
| keep | do nothing destructive; leave worktree and branch as-is |
| discard | typed confirmation (below) → remove worktree (`leo:worktrees`) → force-delete branch |

## Destructive paths require a typed confirmation

`discard`, and any force-delete of a branch with unmerged commits, requires
Leo to type back a confirmation that **names exactly what will be lost** —
not a plain "yes" or "go ahead". Prompt with the specific string, e.g.:

> Type `discard` to delete branch `feature/foo`, 4 commits, no PR — this
> cannot be undone.

- An implied or inferred yes never triggers deletion — silence, "sounds
  good," or approval of some *other* step in the conversation does not
  count.
- If Leo's typed text doesn't match what was asked for, ask again; don't
  guess at intent.
- `keep` never needs this — it's non-destructive by construction.
- If the branch is already merged, force-delete is not "destructive" in the
  data-loss sense (git still warns) — a plain confirmation is enough since
  nothing unmerged is at risk; use the typed-confirmation form when in doubt.

## Leave the repo clean

After any path except `keep`:

- Prune worktree metadata (`leo:worktrees` handles this as part of removal
  — don't leave a stale entry in `git worktree list`).
- Confirm `git status` is clean from the directory you're now in.
- Note the outcome (merged / PR opened + link / kept / discarded) in the
  done report per `leo:verification` — the report's job is to make the
  terminal state legible later, not just at the moment it happened.

## Self-talk to catch

- "The diff was tiny, I basically reviewed it while writing it" — that's
  not a review verdict; go get one.
- "Leo said 'sounds good' earlier, close it out" — sounds-good is not a
  typed confirmation naming what's lost.
- "I'll just clean up the worktree now and merge after" — wrong order,
  breaks the merge step; land first.
- "Discard is obviously right here, I'll skip the prompt to save a round
  trip" — the option set is closed and explicit for a reason; present it.

## Works with

- `leo:worktrees` — owns worktree creation/removal mechanics.
- `leo:verification` — owns the shape of the done report this skill feeds
  its outcome line into.
