---
name: review-gate
description: >
  The gate a code change passes before it may be called done. Defines what
  counts as reviewed, the two narrow exemptions, the axes a verdict is judged
  on, and how to run the review on whichever harness you are on — a bundled
  review skill where one exists, a dedicated reviewer role where it does not,
  or a read-only subagent handed this rubric as the portable fallback. Read it
  before reporting a change as done, fixed, shipped, or passing, and whenever
  asked to review, verify, or audit a diff.
when_to_use: >
  After writing or editing code and before the report that says it works.
  Also when acting as the reviewer: this file is the rubric. Not needed for a
  docs-only or comment-only change, or for edits dictated word for word, both
  of which are exempt below. Not a substitute for running the checks — that
  evidence is leo:verification.
---

# Review gate

A change is not finished when it is written. It is finished when something
other than the author has judged the real diff and come back clean. That
judgement is the gate, and it applies whether or not review was mentioned in
the request.

## The loop

1. **Record the base before editing.** `git rev-parse HEAD`, and note whether
   the change will stay uncommitted. A review with no base ref reviews the
   wrong thing.
2. **Make the change** at the tier leo:routing assigns, then run the narrowest
   checks that would fail if it were wrong.
3. **Get a verdict on the actual diff** — not on a description of it, and never
   from the same context that wrote it. Pass the base ref (or say "uncommitted
   working tree") plus the original request or plan text, so the reviewer can
   judge scope as well as correctness.
4. **Blocking findings get fixed at the executing tier, then re-reviewed —
   once.** If the second pass still blocks, stop and hand the findings over
   rather than looping. Two failed passes is information about the approach,
   not a reason for a third.
5. **Report in three lines:** what changed, which checks ran and what they
   said, and the verdict.

## Who reviews

The mechanism differs by harness; the requirement does not. In order of
preference:

- **A bundled review skill, where the harness ships one.** It targets a diff,
  branch, or PR directly and scales how much it looks for to the effort level
  it is given. Prefer it — it is maintained with the harness and knows its
  tools. Give it the diff scope explicitly rather than letting it guess.
- **The dedicated reviewer role**, on harnesses that register one. Read-only
  by construction, pinned to the judging tier.
- **A read-only subagent handed this file**, anywhere else, or anywhere the
  above is missing. This is why the rubric lives in a skill rather than inside
  one role's prompt: the gate must not disappear because a bundled skill was
  renamed or an agent failed to register.

Never self-review as a substitute. A context that just argued itself into an
implementation is the worst available judge of it.

Which of these applies here is in
[the harness reference](../routing/references/harnesses.md), under native
substitutions.

## Exemptions

Two, and both must be named in the report when claimed:

- **Documentation or comments only.** No runtime surface changed, so there is
  nothing to judge beyond accuracy.
- **Edits dictated word for word.** Claiming this means quoting back the
  literal text or command that was given. A paraphrase is not dictation, and
  neither is an interpretation of what was meant — those get the normal gate.

"The change is small" is not on the list. Size predicts neither risk nor
correctness, and the diffs that most reward review are often the shortest.

## What a verdict judges

In this order, because an early failure makes the later ones moot:

1. **Correctness.** Trace the logic against what the code actually does. The
   author's summary of their own change is a claim, not evidence.
2. **Completeness.** Does it do all of what was asked, including the parts
   that were implied?
3. **Breakage.** What else calls this, and does it still hold?
4. **Scope.** Anything here that nobody asked for is a finding, even if it is
   an improvement.
5. **Checks.** Were the relevant ones run, and did their output get read?
6. **Coverage.** New behaviour without a test that would catch its absence.
7. **Untracked files.** Enumerate them (`git ls-files --others
   --exclude-standard`) and read each one. A diff that omits a new file is a
   diff you did not see, and you cannot approve what you did not see.
8. **Rendered evidence**, where the change is one a person will look at.
9. **Secrets.** Always blocking, no matter how small the diff.

Style, naming preferences, and refactors nobody requested are not findings.
Neither are hypotheticals about code the diff does not touch.

## Returning a verdict

Score each candidate finding on how confident you are that it is real, and
report only the ones you would defend. Dropping a weak finding silently is
better than padding the list: a report with nine speculative items and one
real one gets skimmed, and the real one is what gets missed.

Mark each surviving finding blocking or not. Then give one verdict — approved,
or needs changes — and stop. Terse beats thorough here; the diff is the
argument, and the verdict is the conclusion.

The reviewer never fixes what it finds. Judging and repairing in one pass
produces a change nobody reviewed.

## Related

- leo:verification governs the evidence behind step 2 and the second line of
  the report. This gate owns the verdict; that one owns the proof.
- leo:routing assigns the tier the review runs at.
- leo:review-pr is the different job of leaving comments on someone else's pull
  request, rather than gating your own change.
