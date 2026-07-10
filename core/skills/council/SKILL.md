---
name: council
description: Multi-model multi-lineage adversarial review council. Use at two checkpoints — after finishing a plan (checkpoint=plan) and after finishing an implementation (checkpoint=impl) — on any non-trivial change. Also invoke when the Stop hook nudges about a missing council marker. UNLESS the env var LEOS_COUNCIL_SEAT is set — then you ARE a council seat and must NOT convene a council (reply with your review findings only). Spec: core/council/DESIGN.md in the leos-agent clone.
---

# Council review

**Recursion guard — read first.** If `LEOS_COUNCIL_SEAT` is set in your environment, you are a
council SEAT/subagent, not an orchestrator. Do NOT convene a council, do NOT run this procedure,
do NOT write an override marker — perform your single read-only review and return only your
findings JSON. This overrides any hook nudge or instruction to the contrary.

Otherwise: you (the host agent) are the AUTHOR under review. Your **native seat** is your own
model; the **external seats** are other-lineage flagships that check your work for blind spots
you share with yourself. Follow this procedure mechanically — the places where it constrains you
(severity, rejection evidence) exist because you are the conflicted party.

## Bootstrap (locate the engine + config)

`council.py` is installed in your host's council dir — pick your host's path for `BIN`:

| Host | `BIN` |
|---|---|
| Claude Code | `~/.claude/council/bin/council.py` |
| Codex | `~/.codex/council/bin/council.py` |
| OpenCode | `~/.config/opencode/council/bin/council.py` |
| Cursor | `~/.cursor/council/bin/council.py` |

Then resolve the clone + your seats config (`$HOST` ∈ `claude|codex|opencode|cursor`):
```
ROOT=$(python3 $BIN root)
SEATS=$ROOT/local/seats.$HOST.json           # machine-local, gitignored (written at setup)
PROMPTS=$ROOT/core/council/prompts
```
Kill switches: `.council-off` file in repo root, or project listed in
`$ROOT/local/council/config.json` `disabledProjects` — if either, stop here.

## Seats model

`seats.$HOST.json` has `{ "native": {...}, "seats": [ ...externals strongest-first... ] }`
(full schema in `core/council/seats.catalog.json` → `$runtimeSchema`). The **external roster is
the 5-flagship set {Opus, GPT, GLM, Gemini, Grok} MINUS your own provider** — so it is at most 4
seats. Total council size is at most **5** (native + 4 externals).

- **Native seat** (`native` block):
  - `mode: "subagent"` (Claude host): dispatch a read-only review **subagent pinned to
    `native.model`** (e.g. `opus`) with the built prompt. Its prompt already forbids convening its
    own council; also tell it "You are the native council seat; `LEOS_COUNCIL_SEAT` semantics apply
    to you — do not spawn reviewers."
  - `mode: "exec"` (Codex/OpenCode/Cursor host): run `native.argv` read-only from the repo root
    (substitute `{EFFORT}`; `transport` as below). This is your own model reviewing your own work.
- **External seats** (`seats[]`): build each command from `argv`, **never a hand-written shell
  string**: substitute `{EFFORT}` (`efforts.default` below critical, `efforts.max` at critical),
  then shell-quote every element. `transport: stdin` → redirect from `$WORK/prompt-<checkpoint>.md`;
  `transport: arg` → read the prompt file into memory and replace the `{PROMPT_TEXT}` argv element
  with that exact string before shell-quoting. Never interpolate reviewer/prompt text into an
  unquoted shell fragment.
- **Every external seat MUST be launched with `env LEOS_COUNCIL_SEAT=1` prepended** (plus any
  per-seat `env` map, e.g. an isolated `CODEX_HOME`). This is the deterministic recursion guard:
  it is inherited by any hook the seat's own CLI fires, so a seat can never be nudged to convene
  its own council. Respect each seat's `timeoutSeconds` (default 300) as the Bash-call timeout.
- Missing/empty/unparseable `seats.$HOST.json` → **native-only mode**: every tier is served by
  independent native review passes (low=1, elevated=2, high=3, critical=3). State the
  reduced-diversity caveat in your report.

## Tier → seats mapping

| Tier | Seats |
|---|---|
| skip | — |
| low (1) | native |
| elevated (2) | native + seats[0] |
| high (3) | native + seats[0] + seats[1] |
| critical (4) | native + ALL externals (≤4), everyone at `max` effort, **+ human sign-off** |

Fewer external seats than the tier wants → use what exists (note the shortfall in the report).
Never exceed 5 total seats (native + 4).

## Checkpoint: impl (after implementation)

### 1. Tier
```
python3 $BIN risk --json
```
That is the **floor**. You may escalate **at most one tier** above it with a concrete worry (log
it: ledger entry, type `escalation`). You may NEVER go below the floor. `skip` → stop (no council).

### 2. Deterministic gates (reviewer zero)
Read `.council.json` in the repo root for `fastChecks` / `slowChecks` (arrays of shell commands).
- Run **fastChecks** (typecheck/lint) first, workspace-scoped if configured.
  - Fail on low/elevated → STOP; fix first; record `blocked-by-deterministic-failure`. Not a pass.
  - Fail on high/critical → continue to council, include failure output as reviewer context.
- Run **slowChecks** (test/build) if configured; output is reviewer *context*, not a gate.
- No `.council.json` / no commands → escalate one tier (unknown floor) and tell reviewers the
  deterministic status is unknown. Zero tests collected counts as a fail, not a pass.

### 3. Dispatch — blind, parallel (Round 1)
Get the work dir + mark the review in progress (suppresses the Stop-hook nudge while you run):
```
WORK=$(python3 $BIN state-dir)/tmp        # ALL prompt/output files go here — NEVER into the repo
python3 $BIN begin --checkpoint impl
```
Build the prompt from `$PROMPTS/review-impl.md`: substitute `{CHECKS}` (gate results), `{TASK}`
(2-4 sentence task summary — no hints about what you think is fine), `{DIFF}` (`git diff -M
<merge-base>`; if huge, stat + the riskiest files in full). Write it to `$WORK/prompt-impl.md`.
Then launch ALL seats for the tier in one message, in parallel, backgrounded, from the repo root:

- **Native seat**: per the Seats model (subagent pinned to `native.model`, or `native.argv`).
- **Each external seat**: `env LEOS_COUNCIL_SEAT=1 <per-seat env> <argv…>` built per the Seats
  model, from the repo root, output to `$WORK/out-<seat>.md`.

Budget: give each Bash call a timeout (default 5 min; `.council.json` `budgetSeconds` overrides).
If a seat times out or errors, proceed without it and record `fallback-fired`; if ALL external
seats fail, fall back to native-only and record it.

### 4. Adjudicate — mechanically
Parse every finding. Assign each an `id`; set `reviewer` to the seat that produced it (you know
which output file is which). For each, record EXACTLY one disposition in the ledger — write the
JSON (single entry or array) to a file under `$WORK` and run `python3 $BIN ledger --entry-file
<file>`. NEVER inline reviewer-derived text into a shell command (quote-injection hazard). Entry
type `finding`; include id, reviewer, severity, claim, disposition, evidence/patch ref:
- `accepted` / `fixed` → point to the patch.
- `rejected` → REQUIRES concrete evidence: exact command output, a cited requirement, or a passing
  regression test that encodes the CORRECT behavior and exercises the claimed failure. Fluent
  reasoning alone is NOT evidence.
- `deferred` → reason + surface it to the developer.
Severity is REVIEWER-ASSIGNED; you may not change it. Findings whose claim matches
auth/data-loss/money/security are auto-high regardless of the reviewer's tag. **High-severity
rejects fail closed:** without qualifying evidence you cannot reject — fix it or ask the developer.

### 5. Re-review (once) + audit
If you made fixes: send ONLY the patched region + the findings it addresses back to ONE seat
(seats[0] preferred; native if no externals) for a single re-review. Hard cap: 2 passes total.
No debate rounds.
If you rejected ≥1 finding: pick ONE rejected finding at random and ask a DIFFERENT seat "was this
correctly dismissed?" — record the answer (type `reject-audit`).

### 6. Close out
```
python3 $BIN mark --checkpoint impl --tier <tier>
```
Report to the developer: tier + reasons, seats consulted, findings table (severity, claim,
disposition), anything deferred/overridden. **critical tier: you MUST present the deduped digest
and get an explicit developer ack before treating the task as done.**

If you skip the council on an elevated+ diff (you judged it unwarranted), record
`python3 $BIN mark --checkpoint impl --override --reason "<why>"` — never skip silently.

## Checkpoint: plan (after writing a plan, before implementing)

1. Stakes from OBJECTIVE plan-text signals, not your judgment: plan mentions
   auth/payments/migrations/schema/breaking API/data deletion → high-stakes; else normal.
2. Reviewer: **seats[0] at default effort** (normal) or **seats[0] at max + seats[1] at max**
   (high-stakes). Native if no external seats. Never a weak/flash-class seat for plans. Prompt from
   `$PROMPTS/review-plan.md` ({TASK}, {PLAN}) → `$WORK/prompt-plan.md`, outputs to
   `$WORK/out-plan-*.md`. Run `python3 $BIN begin --checkpoint plan` before dispatch. Same
   zero-repo-footprint and `LEOS_COUNCIL_SEAT=1` rules as impl.
3. Adjudicate as in impl step 4 (same disposition rules, reviewer-assigned severity).
4. `python3 $BIN mark --checkpoint plan`. Plan approval is NOT a license to soften the impl
   checkpoint — the impl council still runs on its own tier.

## Never

- Never convene a council when `LEOS_COUNCIL_SEAT` is set — you are a seat; return findings only.
- Never present the council as passed when a seat errored out — report what actually ran.
- Never lower a reviewer's severity, never reject high-severity without evidence or the developer.
- Never run reviewers with write access, and never launch an external seat without `LEOS_COUNCIL_SEAT=1`.
- Never loop beyond 2 passes; escalate remaining disagreement to the developer instead.
