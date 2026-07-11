---
name: council
description: Run Leo's explicit, risk-tiered multi-model review at the plan or implementation checkpoint.
---

# Leo's Agents council

This skill is invoked only by the **orchestrator** after it decides to review a plan or an
implementation. It never starts a council merely because the runner exists.

If `LEOS_COUNCIL_SEAT=1` is set, you are already a council seat. Return your review; do **not**
invoke this skill, `runner.py`, or another Leo's Agents council. Ordinary host tools/subagents are
allowed when appropriate—the prohibition is specifically on nested Leo council orchestration.

## Locate the private runtime

Select the host name (`claude`, `codex`, `opencode`, or `cursor`) and its installed council binary:

| Host | Binary |
|---|---|
| Claude Code | `~/.claude/council/bin/council.py` |
| Codex | `${CODEX_HOME:-~/.codex}/council/bin/council.py` |
| OpenCode | `~/.config/opencode/council/bin/council.py` |
| Cursor | `~/.cursor/council/bin/council.py` |

The installed `BIN` is Python, so derive the clone deterministically and use its private runtime:

```
ROOT=$(dirname "$(dirname "$(dirname "$(dirname "$(realpath "$BIN")")")")")
RUNTIME="$ROOT/bin/leos-python"
ENGINE="$ROOT/core/council/bin/council.py"
RUNNER="$ROOT/core/council/bin/runner.py"
SEATS="$ROOT/local/seats.$HOST.json"
PROMPTS="$ROOT/core/council/prompts"
```

If `$RUNTIME` is unavailable, stop and ask the installer to run:

```
python3 "$ROOT/bin/leos-runtime.py" setup
```

Do not use ambient `python3` for Leo scripts. All council state, prompts, CLI output, locks, and
temporary files belong under gitignored `$ROOT/local/`.

Kill switches: stop if the target repository root has `.council-off`, or its path is in
`$ROOT/local/council/config.json` `disabledProjects`.

## Determine the tier

```
"$RUNTIME" "$ENGINE" risk --json
```

That risk is the floor. You may escalate one tier with a concrete ledger entry; never lower it.
`skip` means no council. The tier mapping is:

| Tier | Seats |
|---|---|
| low | native |
| elevated | native + first configured external |
| high | native + first two configured externals |
| critical | native + all configured externals + developer sign-off |

Missing/invalid seats config means **native-only**, and the report must say diversity was reduced.
`local/seats.<host>.json` contains resolved model slugs and argv arrays; never commit it or copy its
values into tracked files.

In native-only fallback, retain independent-review depth: low runs one native pass; elevated runs
two; high and critical run three. A native `mode: subagent` yields one
`orchestrator-native-subagent-required` record for each pass; do not collapse them into one review.

Plan checkpoints use a different, external-first selection: normal (`low`/`elevated`) plans use
the first configured external seat; high-stakes (`high`/`critical`) plans use the first two. The
native seat is used only when no external seat is configured, and then only once. This preserves an
independent planning review without turning plan review into an implementation panel.

## Prepare context and deterministic checks

Run task-relevant deterministic checks explicitly and include their results as reviewer context.
The engine deliberately does not execute commands sourced from `.council.json`; that file controls
risk globs, default branch, and thresholds only. A failed relevant check blocks low/elevated work;
for high/critical include the failure as reviewer context. Missing verification is reported as
unknown, never silently treated as passing.

Create a private prompt file under local state. It contains the task summary, check results, and
the appropriately bounded diff. Do not include `.env`, private keys, credential values, or other
secrets. The runner independently refuses likely credential material unless `--redact-sensitive`
is used, in which case the whole prompt is withheld before any seat sees it (a PEM or sensitive-file
marker can cover later lines); raw matched prompts cannot be force-sent. Prefer constructing a
useful sanitized prompt yourself.

For example:

```
WORK=$("$RUNTIME" "$ENGINE" state-dir)/tmp
mkdir -p "$WORK"
PROMPT="$WORK/prompt-impl.md"
# Write the chosen review template with {TASK}, {CHECKS}, and {DIFF} substituted into $PROMPT.
```

For a plan, use `review-plan.md` and `prompt-plan.md`. For implementation, use `review-impl.md`.

## Dispatch through the runner

The runner is the only CLI dispatcher. It creates the active marker **before** process launch,
passes `LEOS_COUNCIL_SEAT=1` to every external seat, captures stdout/stderr privately, applies a
process-group timeout, and emits a typed result. It does not use a shell or interpolate review text
into a command line except where a chosen legacy `arg` transport explicitly requires it.
It also appends private `events.jsonl` lifecycle records and emits terse stderr start/finish
progress, so a long tool loop cannot look indistinguishable from a blank return.
Each CLI seat runs in a private scratch directory under the run's work dir with its own synthetic
Git root (per-seat `"cwd": "repo"` opt-out). The project-root boundary prevents parent repo
instructions from loading even when this clone reviews itself. The runner injects the reviewed
repo's absolute path as a prompt header — the orchestrator's prompt template does not need to
include it, and the header is added even for a redacted prompt.

```
"$RUNTIME" "$RUNNER" run \
  --host "$HOST" --checkpoint impl --tier high --prompt "$PROMPT" --cwd "$PWD" \
  --approve-external
```

Use the actual checkpoint/tier. `--approve-external` means the developer/project policy has
explicitly approved sending this prompt to the named providers; omit it and the runner refuses
external dispatch. The command is explicit orchestrator action, not a daemon. Its result file is
under `$ROOT/local/council/work/.../result.json`.

- An `exec` native seat is run by the runner.
- A native `mode: subagent` result is `orchestrator-native-subagent-required`; dispatch exactly one
  native read-only subagent with the returned private `promptPath`. Tell it not to convene Leo's
  Agents council. It may otherwise use ordinary allowed tools/subagents. Save its mandatory
  findings JSON privately and run `runner.py collect-native --result <result.json> --seat <name>
  --review-file <file>` for each pending native pass. Until collection, the runner reports
  `dispatchOk: true` but `reviewComplete: false`; never present it as complete.
- `completed` is the only successful CLI response. `empty-output`, `missing-review-content`,
  `invalid-structured-output`, `invalid-review-findings`, `unsupported-adapter`, `nonzero-exit`,
  `timed-out`, `cancelled`, `signal-exit`, `invalid-seat-config`, `isolation-error`, `unavailable`,
  and `execution-error` are distinct failures. Report each one; never infer a successful review
  from a blank terminal or a transport bookkeeping event with no reviewer message.
- If all external seats fail, continue native-only and append a `fallback-fired` ledger entry. Do
  not state that the council passed.

An active marker owned by another run, or `LEOS_COUNCIL_SEAT=1`, makes the runner refuse with
`nested-leos-council-refused`. This is intentional recursion prevention, not a retry signal.

## Adjudicate and close

Parse each seat's JSON findings. Preserve reviewer severity. Record exactly one disposition for
every finding in a private JSON file, then ledger it without placing reviewer text in shell args:

```
"$RUNTIME" "$ENGINE" ledger --entry-file "$WORK/dispositions.json"
```

- `fixed` / `accepted`: cite the patch.
- `rejected`: require a concrete command result, requirement, or correct regression test.
- `deferred`: explain it to the developer.
- Never reject a high-severity finding without qualifying evidence; fix it or ask the developer.

After fixes, send only the affected patch/finding to one seat for one re-review. Maximum two total
passes—no debate loops. Sample one rejected finding with a different seat when any rejection exists.
The re-review is a first-class runner pass:

```
"$RUNTIME" "$RUNNER" run \
  --host "$HOST" --checkpoint impl --tier <tier> --prompt "$PROMPT_FIX" --cwd "$PWD" \
  --approve-external --follow-up --seat <seat-name>
```

`--follow-up` reuses the active run's marker and run id and writes under `<run>/pass-2/` — round-1
artifacts stay immutable — and the runner refuses a third pass (`follow-up-passes-exhausted`) and
refuses reusing a finished `--run-id` without it (`run-id-work-exists`). `--seat` is also how the
different-seat reject-audit is dispatched. If the marker's TTL lapsed mid-fix, `--follow-up`
returns `no-active-run-for-follow-up`; dispatch a fresh run instead.

Finally close the active marker and write the reviewed baseline, passing the run id from
`result.json` so another run's fresh marker can never be closed by mistake:

```
"$RUNTIME" "$ENGINE" mark --checkpoint impl --tier <tier> --run-id <runId>
# critical tier additionally requires: --signoff "<developer acknowledgement>"
```

For an intentional skip on elevated+ work, record the explicit override instead. Critical work
requires a deduplicated digest and explicit developer acknowledgment **recorded via `--signoff`**
before completion — `mark` exits 1 without it (the effective tier is the max of the computed risk
and the requested tier, so a critical-scoring diff cannot bypass it by requesting a lower tier).

## Never

- Never automatically convene a council or run one from a council seat.
- Never pass prompt/finding content through a hand-built shell command.
- Never treat no output, a timeout, or an invalid structured response as a review.
- Never send likely secrets to an external seat by default.
- Never write Leo runtime data outside `$ROOT/local/`.
