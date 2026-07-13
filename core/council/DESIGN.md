# Council Review — Design Spec

A multi-model, multi-lineage adversarial review harness that works across agentic hosts (Claude
Code, Codex, OpenCode, Cursor). The **host session's own model is the AUTHOR**, and the **host's
own-provider seat** is one element of the unified `seats[]` array (on Claude Code that seat is
`mode: subagent` pinned to the Opus line; on other hosts it's a `mode: exec` subprocess reusing the
host's login, pinned per the OpenAI flavor rule in §2 — so the own-provider reviewer may differ
from the session's model); a council of **other-lineage flagships** checks the work at two
checkpoints — after **planning** and after **implementation**. The orchestrator reads their
findings, adjudicates mechanically, fixes, and re-reviews once. Goal: catch training-lineage-correlated blind spots the
author shares with itself.

The engine (`bin/council.py`) is **host-agnostic** — it scores the diff and manages
markers/ledger/Stop-hook. The explicit adapter runner (`bin/runner.py`) selects configured CLI
seats, owns process lifecycle/output collection, and writes typed results. The orchestrator still
chooses when to call it and owns `mode: subagent` dispatch. Both are data-driven from
machine-local `local/seats.<host>.json`.

---

## 1. Design principles

1. **The author must not decide how hard it gets checked.** The gate is the diff-derived risk
   floor. Reviewer confidence is finding metadata, not an author control that rewrites the tier.
2. **Diversity first, even on the common path.** The everyday (tier-1) reviewer is a *foreign*
   lineage — the own-provider seat is never the sole tier-1 reviewer, so the routine same-lineage
   self-check the author would otherwise get is replaced by one cross-lineage seat. The own provider
   and further foreign lineages join as stakes rise. Cost still tracks risk (one reviewer on the
   common path, the full panel only when it earns its cost), but that one reviewer is a different
   lineage than the author. If no foreign seat is reachable, tier-1 falls back to the own-provider
   seat and the run is flagged `reducedDiversity` — never presented as a full-diversity pass.
3. **Cost tracks risk.** Most changes get one reviewer; the full panel is rare.
4. **Never auto-trigger.** The council runs only when the orchestrator invokes the skill/runner;
   a Stop hook is a soft reminder inside the finish-the-task flow and surfaces findings
   in the report + a boundary digest. The Stop hook is a soft nudge, never a hard git gate.
5. **Adjudication is externally verifiable, not self-refereed.** Severity comes from the reviewer;
   high-severity rejections fail *closed* without evidence; a different reviewer spot-checks a
   sample of rejections.
6. **Prove and tune with data.** Every finding + disposition is logged to a per-project ledger.
7. **A terminal state is evidence.** Blank output, structured bookkeeping with no reviewer message,
   malformed structured output, a nonzero exit, cancellation, timeout, an invalid seat/adapter
   configuration, and an externally signalled seat are failures with distinct records, never an
   implicit clean review.

---

## 2. The roster (seven flagship roles, unified `seats[]`)

A fixed roster of seven flagship **roles** — the exact model slug is resolved at **setup**, never
committed (recipes carry a `{MODEL}` placeholder; see `seats.catalog.json`). Every reviewer —
including the host's own-provider seat — is one element of a single `seats[]` array in
`local/seats.<host>.json`. There is **no top-level `native` object**: dispatch is per-seat `mode`
in {`subagent`, `exec`} (the old `mode: native` is gone).

| Role | Provider | Transports (best → fallback) | Read-only | Recursion isolation |
|---|---|---|---|---|
| **Opus** | Anthropic | subagent (Claude host) → `claude` → `cursor` → `opencode` | plan mode | `--safe-mode` (real, on `claude`/subagent) |
| **GPT** | OpenAI | `codex` → `cursor` → `opencode` | sandbox read-only | sentinel + ephemeral session |
| **Grok** | xAI | `cursor` → `opencode` | plan mode | runner scratch cwd |
| **GLM** | Zhipu | `cursor` → `opencode` | plan mode | runner scratch cwd |
| **Gemini** | Google | `cursor` → `opencode` | plan mode | runner scratch cwd |
| **MiMo** | Xiaomi | `opencode` only | plan agent | runner scratch cwd |
| **DeepSeek** | DeepSeek | `opencode` only | plan agent | runner scratch cwd |

- **The host's own-provider seat is one element of `seats[]`, not a first-class `native` block.**
  On a Claude Code host the Opus seat is `mode: subagent` — an in-process read-only Agent subagent
  **pinned to Opus** (`model: opus` — the Opus line specifically, never Fable/Mythos); the runner
  reports it as `orchestrator-subagent-required` and the orchestrator dispatches it and folds it
  back via `runner.py collect-subagent` (the `collect-native` alias is kept for back-compat). On
  every other host the own-provider seat is `mode: exec` — a runner subprocess reusing the host's
  login (Codex-on-Codex, Cursor-on-Cursor, OpenCode-on-OpenCode).
- **`mode: exec` covers foreign-provider seats AND own-provider seats on non-Claude hosts.** Doctor
  reports `kind: "exec"` for these (was `"external"`).
- **Best installation effort.** At setup attempt all seven roles; install a seat only if its best
  available transport is installed AND its driver smoke passes; silently drop the rest.
- **The Anthropic role is always the Opus line** (alias `opus` tracks the latest Opus) — never the
  Claude-5 / Mythos-class line (Fable, Mythos).
- **The OpenAI role uses the most capable flavor of the newest GPT generation** (Leo's standing
  rule). GPT-5.6 ships three capability flavors — Sol > Terre > Luna, new names in 5.6, not
  lineages — so 5.6 resolves to Sol, never Terre or Luna. A newer GPT generation supersedes 5.6
  automatically and its most capable flavor is selected; the exact supported slug remains
  machine-local.
- **No runtime model discovery.** Setup resolves slugs and writes them to `local/seats.<host>.json`.
- **Reviewers run with the transport's read-only restrictions** and may read/grep the repo to
  verify claims, but never modify it. OpenCode/Cursor plan modes are capability requests, not an
  absolute OS-level containment guarantee; their setup smoke tests are required.
- **Reviewers may use ordinary tools/subagents**, but no seat may convene a nested Leo's Agents
  council (see §8).

---

## 3. The gate (hybrid, capped + logged)

The final tier is the risk floor computed by `council.py risk` from the diff (path/shape + semantic
signals—risk-path globs, blast radius, deletions, new deps/env, exported-API changes, removed
assertions, security symbols, and config surface). The floor is a proxy, not tamper-proof; the
ledger and sampled audit are the compensating controls.

**Escalate only on a genuinely undeterminable base** (a git-diff failure or a diff past the parse
cap) — a repo with no upstream/remote is NOT undeterminable: diffing against HEAD is a legitimate
base and is not escalated (so a local, remoteless branch isn't perpetually over-tiered).

**Delta-gated re-review.** Once a checkpoint is reviewed, `council.py mark` records a `git write-tree`
snapshot of the reviewed worktree as the **reviewed baseline**. The Stop hook then scores the
*increment since that baseline*, not the cumulative branch — so a small follow-up fix on top of a
large reviewed change is judged on its own (small) risk and does not re-trigger a full council. Only
a genuinely non-trivial increment re-nudges.

---

## 4. The ladder

Selection is per-seat **`minTier`** (integer 1..4, default 4), read by the engine ONLY from the
installed `local/seats.<host>.json` — never assumed from the catalog. A seat runs at council tier T
iff `seat.minTier <= T`. The catalog's `presets.minTier` block is a setup-time default the installer
stamps onto each seat (a hand-edited seats file may override it freely), and it is **diversity-first**:
the own-provider seat is never the sole tier-1 reviewer — it gets minTier 2, and the strongest
reachable *foreign* lineage gets minTier 1. On a Claude host that means GPT=1, Opus=2, grok=3,
glm/gemini/mimo/deepseek=4; on another host the own-provider and lead-foreign roles swap (Codex:
Opus=1, GPT=2). If no foreign seat is reachable, the own-provider seat falls back to minTier 1 and
setup warns of reduced diversity.

| Tier | Index | Seats that run (Claude-host default) | Effort | Fires when |
|---|---|---|---|---|
| **skip** | — | — | — | docs/comments/formatting/lockfile-only, or no code change; also when zero seats are configured |
| **low** (1) | 1 | lead-foreign (GPT on Claude; Opus elsewhere) | default/high | small, isolated, no risk signals |
| **elevated** (2) | 2 | + own-provider (Opus on Claude) | high · default | moderate blast radius / new deps / deletions / feature w/o tests |
| **high** (3) | 3 | + grok | high/xhigh · max | risk globs or large blast radius or semantic risk |
| **critical** (4) | 4 | all configured seats **+ human sign-off** | max per seat | auth/migrations/payments/public-API + high blast radius, or data-loss |

Critical requires human sign-off **by default**: the orchestrator synthesizes the reviews into
**one deduped digest** and asks the developer to ack before considering the change done. This is
the one hard gate; `requireSignoffAtCritical: false` in the machine-local config (§7) turns it off.

---

## 5. Execution & convergence

Deterministic gates first (fast = prerequisite, slow = reviewer context; gate-absent → escalate one
tier; gate-vacuous → treat as fail). Round 1 blind & parallel; each seat tags its own severity.
The runner uses direct argv execution, private bounded stdout/stderr capture, process-group
timeouts, and adapter-specific structured-output extraction for `claude`, `codex`, and `opencode`.
Its vendor-neutral `start`/`status`/`stop` lifecycle is the default orchestration path: a detached
runner owns a new process session, survives a host tool-call deadline, remains pollable through
private events/results, and can still be cancelled through a private request followed by typed
seat-process-group teardown. `start` runs the same follow-up preconditions as `run` (no active run,
checkpoint mismatch, missing first pass, passes exhausted) so a bad launch is typed and leaves no
orphan work dir; `status` and `stop` auto-detect a dispatched `pass-2` from its `launcher.json`,
making `--follow-up` optional for them. Synchronous
`run` remains a compatibility interface.
Cursor is only accepted after setup confirms a usable output contract; otherwise it is unavailable,
not silently treated as a review.

When no seat's `minTier` qualifies at the tier (or zero seats are configured), **reduced-diversity
fallback** runs the single lowest-`minTier` configured seat once, emits a `fallback-fired` event,
and the report must state diversity was reduced. Diversity = count of distinct `provider` values
among selected seats; fewer than 2 distinct providers is reduced diversity. **The runner computes
this over the selected set on every run** (not only the empty-selection fallback) and surfaces
`reducedDiversity: true` + `diversityProviders` in `result.json` plus a `reduced-diversity` event —
so a single-provider selection (e.g. tier 1 with only the own-provider seat) can never be reported
as a full-diversity pass. If zero seats are configured, the council is skipped (`skip`). This
replaces the former "native-only fallback"; it is
a reduced-diversity caveat, not a claim that one self-review substitutes for a panel.

Plan checkpoints use the **same `minTier <= T` filter** as impl — plan no longer has a separate
external-first rule. The runner applies one selection per checkpoint.
Mechanical adjudication (`accepted`/`fixed`/`rejected`/`deferred`); a `rejected` finding needs
concrete evidence (command output, cited requirement, or a passing regression test encoding the
CORRECT behavior); high-severity rejections fail closed. One bounded re-review (2 passes total, no
debate). Sampled reject-audit by a different seat. Per-seat wall-clock timeouts; exec seats may
carry explicit longer per-checkpoint overrides — `planTimeoutSeconds` and `implTimeoutSeconds`
(the catalog sets both to 600 because impl reviews explore the actual diff) — neither of which
changes the reduced-diversity-fallback deadline. On exceed, fall
back to a single strong reviewer and record `fallback-fired`.

---

## 6. Triggering, surfacing & ledger

- **Skill** invoked at both checkpoints. Planning uses a strong reviewer (never a flash-class seat);
  plan-stakes from objective plan-text keywords. Plan approval is not license to skip the impl
  checkpoint.
- **Implementation** backstopped by a soft `Stop`-hook nudge — if a turn ends on an `elevated`+
  diff with no fresh marker, the hook reminds the orchestrator. Never a hard block; override allowed
  with a logged reason.
- **State (shared, gitignored clone footprint):** `ledger.jsonl` + fixed-name pointers live under
  `$ROOT/local/council/state` by default. This intentionally makes all Leo-specific runtime data
  (including venv, prompts, results, locks, and temp indexes) inspectable and removable with the
  clone; it does not use `/tmp` or `~/.local/state`. The pointers are
  hash-independent (they survive diff-hash churn as the author iterates):
  - `baseline-<checkpoint>.json` — the reviewed-tree snapshot the delta-gate diffs against.
  - `in-review.json` — written by `council.py begin` at dispatch; suppresses the nudge while a
    council runs (TTL 30 min).
  - `nudge-state.json` — the persistent loop guard (see §8).
  - `cache.json` / `base-cache.json` — a short-TTL risk cache and a HEAD-keyed base cache, so
    repeated Stop events in a turn don't recompute from scratch.
  Legacy `markers/<diff-hash>.json` are still written by `mark`/`begin` for cross-tool/back-compat
  visibility, but the hook's decision keys on the pointers above, not the exact diff hash.

  The prior `~/.local/state/leos-agent/council/state` location is never imported automatically.
  Doctor surfaces it, and `council.py migrate-legacy-state` performs an explicit source-preserving
  copy only into an empty clone-local state target.

---

## 7. Scope & configuration

Global but risk-gated (dormant on trivial work). Per-project off-switch: `.council-off`. Machine
config: `local/council/config.json` (`disabledProjects`; plus `requireSignoffAtCritical`, default
`true` — set `false` to drop the one hard gate so critical still convenes every seat and produces
the digest but no longer blocks on a manual `--signoff` ack, keeping the council a soft nudge at
every tier). Valid per-project `.council.json` fields are `riskGlobs`, `defaultBranch`, and the
four documented `thresholds`. Deterministic checks are orchestrator-owned prompt inputs; the engine
never executes repository commands from config.

---

## 8. Anti-recursion (a seat never convenes its own council)

Layered, deterministic-first — tool-agnostic across the available hook/plugin/skill surfaces:

1. **Env sentinel `LEOS_COUNCIL_SEAT=1`** — the runner sets it on every `mode: exec` seat launch;
   child hooks the seat's CLI fires inherit it.
2. **Hook check** — `council.py hook` returns 0 immediately if `LEOS_COUNCIL_SEAT` is set, so a
   seat is never nudged.
3. **Skill/runner self-check** — a seat and the runner both refuse a nested Leo council.
4. **Shared local state + owned in-review marker** — the runner records a run id before dispatch;
   a second run with another id is refused while the marker is fresh. This covers env-stripping
   CLIs and cross-tool visibility.
5. **Prompt clause** — prompts prohibit Leo council recursion, while permitting ordinary subagents
   when the transport allows them.
6. **Mechanical isolation per seat** — Claude `--safe-mode` (disables CLAUDE.md/skills/hooks/MCP);
   Codex `--ephemeral --sandbox read-only`; OpenCode/Cursor `--agent plan`/`--mode plan`. Every CLI
   seat additionally launches in a **runner-provided scratch directory under `local/` with its own
   synthetic Git root** (removed after the seat). The distinct project root prevents repo-local
   agent config (`.cursor/rules`, `AGENTS.md`, OpenCode project config) from gaining instruction
   authority inside a reviewer even when this clone reviews itself; the reviewed repo's path travels
   in a prompt header. A per-seat `"cwd": "repo"` opt-out exists for transports that cannot read
   outside their workspace, with that injection risk documented. Only Claude has a true
   `--safe-mode`; the others rely on read-only + project-root/cwd/env hygiene.

Registration keeps only the `Stop` event (never `SubagentStop`), so subagent seats are never
hook-nudged. Bounds retained as backstops: a **persistent loop guard** (`nudge-state.json`, scoped
per project+checkpoint, so it survives diff-hash churn across edits — capped at `MAX_NUDGES`, cleared
by a real `mark`, and re-armed at most `MAX_REARMS` times only if genuinely new substantial work
appears since it tripped), read-only seats, per-seat timeouts, hard 2-pass cap.

---

## 9. Explicitly rejected alternatives

Self-assessed confidence as primary gate (conflict of interest); flash-class as everyday baseline
(under-catches); orchestrator-assigned severity (defangs disposition); mid-flow hard-block
(churn); "failing test to reject a finding" (backwards); committing model slugs (goes stale).
