---
name: verification
description: >
  Fresh-evidence gate before claiming done, fixed, or passing. A completion
  claim needs a proving command run in the current turn whose output was
  actually read — never a prior run, never "should pass now", and never a
  subagent's self-report relayed as fact.
when_to_use: >
  Before writing or relaying any completion claim. Not for in-progress status
  updates that claim nothing, and not a substitute for leo:review-gate, which
  owns the verdict this evidence sits under.
---

# verification

Core rule: no completion claim without a proving command run fresh, this
turn, with output actually read. A claim resting on memory, a prior run, or
someone else's word is not verification — it is a guess wearing the shape
of one.

## When it fires

Any sentence of the form "X passes," "X is fixed," "X works now," "agent Y
finished." That sentence is a claim. A claim needs proof, and proof has an
expiration: the moment code changes again, prior proof is stale.

Does not fire for: status updates that don't assert completion ("still
running," "found the bug, fixing now"), or work that genuinely has no
runtime surface (docs/comment-only diffs — see the execute-then-review
exemptions).

## The discipline

1. **Name the command that would falsify the claim.** Not "does it look
   right" — the specific test/build/repro that fails if the claim is false.
   If no such command exists, the claim isn't verifiable yet; say so instead
   of asserting it.
2. **Run it fresh, this turn.** A green run from three edits ago proves
   nothing about the code as it stands now.
3. **Read the exit status and failure counts.** Not the last line of
   scrollback, not a summary a subagent wrote — the actual output.
4. **Only then claim, and state the evidence** — the command and what it
   returned, not just "verified."

Skipping step 1 is how "should be fine" sneaks in. Skipping step 2 is how a
stale green run gets relayed as current. Skipping step 3 is how a nonzero
exit gets read as success because the output scrolled by fast.

## Claim → proof

| Claim | Falsifying command |
|---|---|
| Tests pass | the actual test command, run now, exit code + failure count read |
| Build is green | the build command, run now, read for errors/warnings |
| Bug is fixed | the reproducer that showed the bug — now green, run fresh |
| Agent reports done | its diff and output, inspected directly — its "success" is a claim, not evidence |
| This library call is correct | the installed package or current docs, read this session — see leo:freshness |
| A UI change looks right | a render produced after the edit, looked at — see leo:visual-verification |

## Subagent reports are claims, not evidence

A subagent saying "success," "all tests pass," or "implemented as
requested" is exactly as unverified as your own untested assertion would be
— it is one more claim to check against artifacts. Inspect the diff it
produced. Run the check it says it ran. If it reports a test command, that
command's output belongs in your evidence, not its summary of the output.
Relaying a subagent's self-report upward without this check just moves the
gap in provenance one level up the chain.

## Done is the three-line report

Writing code is not done. Done is the execute-then-review report:

- **what changed** — the diff, in one line
- **checks run** — the fresh commands from this gate, with results
- **review verdict** — clean per the execute-then-review policy, not
  self-assessed

Verification is the evidence behind lines two and three. A report with line
one but not two and three is a status update, not a completion claim — label
it as such.

## Self-talk to catch

- "It should pass now, I fixed the obvious thing" — run it.
- "The tests were green before this edit" — before this edit is not now.
- "The subagent said it's done" — done according to whom, checked how.
- "I read the code and it looks correct" — reading is not running.
- "Re-running is wasteful, nothing changed" — if nothing changed, the prior
  run is fine to cite as fresh evidence; if anything did, it isn't.

## Works with

- leo:review-gate — that gate is the outer loop this skill feeds into. It
  owns the verdict on the diff; this skill governs the evidence claimed on
  the way there.
- The verdict that comes back is itself a claim to relay accurately, not to
  soften or summarize away.
- End-to-end exercise — when the falsifying command is "does the real flow
  work," drive the actual app or flow, not just the test suite.
- leo:freshness — this gate proves the code you wrote runs; that one governs
  whether the third-party API you wrote it against actually exists. A green
  test against a mocked dependency clears this skill and not that one.
- leo:visual-verification — for a change someone sees, the falsifying artifact
  is a render, not an exit status. That skill owns what the render must show.
