# Council Review — Design Spec

A multi-model, multi-lineage adversarial review harness that works across agentic hosts (Claude
Code, Codex, OpenCode, Cursor). The **host session's own model is the AUTHOR**, and the **host's
provider supplies the native reviewer** (on Claude Code that seat is pinned to the Opus line, on
Codex to the OpenAI flavor rule in §2 — so the native reviewer may differ from the session's
model); a council of **other-lineage flagships** checks the work at two checkpoints — after
**planning** and after **implementation**. The orchestrator reads their findings, adjudicates
mechanically, fixes, and re-reviews once. Goal: catch training-lineage-correlated blind spots the
author shares with itself.

The engine (`bin/council.py`) is **host-agnostic** — it scores the diff and manages
markers/ledger/Stop-hook. The explicit adapter runner (`bin/runner.py`) selects configured CLI
seats, owns process lifecycle/output collection, and writes typed results. The orchestrator still
chooses when to call it and owns native host-subagent dispatch. Both are data-driven from
machine-local `local/seats.<host>.json`.

---

## 1. Design principles

1. **The author must not decide how hard it gets checked.** The gate is the diff-derived risk
   floor. Reviewer confidence is finding metadata, not an author control that rewrites the tier.
2. **Capability first on the common path; diversity where stakes are high.** The strong native
   model is the everyday baseline; foreign lineages are added as stakes rise, where their different
   failure distributions earn their cost.
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

## 2. The roster (host = native; the other four flagships = external)

A fixed roster of five flagship **roles** — the exact model slug is resolved at **setup**, never
committed (recipes carry a `{MODEL}` placeholder; see `seats.catalog.json`):

| Role | Provider | Transport (default) | Read-only | Recursion isolation |
|---|---|---|---|---|
| **Opus** | Anthropic | `claude --safe-mode --no-session-persistence --print` | plan mode | `--safe-mode` (real) |
| **GPT** | OpenAI | `codex exec --ephemeral --sandbox read-only -m {MODEL}` | sandbox read-only | sentinel + ephemeral session |
| **GLM** | Zhipu | `opencode run --agent plan -m openrouter/{MODEL}` | plan agent | runner scratch cwd |
| **Gemini** | Google | `opencode run --agent plan -m openrouter/{MODEL}` | plan agent | runner scratch cwd |
| **Grok** | xAI | `cursor-agent -p --mode plan` *or* `opencode … openrouter/{MODEL}` | plan mode | runner scratch cwd |

- **The host's provider supplies the native reviewer.** For Claude Code the native seat is a
  read-only subagent **pinned to Opus** (`model: opus` — the Opus line specifically, never
  Fable/Mythos). For Codex it is a `codex exec` read-only pass pinned at setup per the OpenAI
  flavor rule below; for OpenCode/Cursor a `--agent plan` / `--mode plan` self-pass.
- **External seats = roster minus the host's own provider.** Claude Code host → {GPT, GLM, Gemini,
  Grok}; Codex host → {Opus, GLM, Gemini, Grok}; etc. So the council is at most native + 4 = 5.
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

| Tier | Council (cumulative) | Effort | Fires when |
|---|---|---|---|
| **skip** | — | — | docs/comments/formatting/lockfile-only, or no code change |
| **low** (1) | native | native default/high | small, isolated, no risk signals |
| **elevated** (2) | native + seats[0] | native high · external default | moderate blast radius / new deps / deletions / feature w/o tests |
| **high** (3) | native + seats[0] + seats[1] | native high/xhigh · externals max | risk globs or large blast radius or semantic risk |
| **critical** (4) | native + all externals (≤4) **+ human sign-off** | max per seat | auth/migrations/payments/public-API + high blast radius, or data-loss |

Critical requires human sign-off: the orchestrator synthesizes the reviews into **one deduped
digest** and asks the developer to ack before considering the change done.

---

## 5. Execution & convergence

Deterministic gates first (fast = prerequisite, slow = reviewer context; gate-absent → escalate one
tier; gate-vacuous → treat as fail). Round 1 blind & parallel; each seat tags its own severity.
The runner uses direct argv execution, private bounded stdout/stderr capture, process-group
timeouts, and adapter-specific structured-output extraction for `claude`, `codex`, and `opencode`.
Cursor is only accepted after setup confirms a usable output contract; otherwise it is unavailable,
not silently treated as a review.

When there are no external seats at all, native-only fallback preserves independent review depth:
low = one pass, elevated = two, high/critical = three. This is reduced-diversity fallback, not a
claim that one self-review substitutes for a panel.

Plan checkpoints are external-first: one configured external reviewer on normal plans, two on
high-stakes plans, falling back to one native pass only when there is no external seat. The runner
applies that selection separately from the implementation tier ladder.
Mechanical adjudication (`accepted`/`fixed`/`rejected`/`deferred`); a `rejected` finding needs
concrete evidence (command output, cited requirement, or a passing regression test encoding the
CORRECT behavior); high-severity rejections fail closed. One bounded re-review (2 passes total, no
debate). Sampled reject-audit by a different seat. Per-seat wall-clock timeouts; on exceed, fall
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
config: `local/council/config.json` (`disabledProjects`). Valid per-project `.council.json` fields
are `riskGlobs`, `defaultBranch`, and the four documented `thresholds`. Deterministic checks are
orchestrator-owned prompt inputs; the engine never executes repository commands from config.

---

## 8. Anti-recursion (a seat never convenes its own council)

Layered, deterministic-first — tool-agnostic across the available hook/plugin/skill surfaces:

1. **Env sentinel `LEOS_COUNCIL_SEAT=1`** — the runner sets it on every external-seat launch;
   child hooks the seat's CLI fires inherit it.
2. **Hook check** — `council.py cmd_hook` returns 0 immediately if `LEOS_COUNCIL_SEAT` is set, so a
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

Registration keeps only the `Stop` event (never `SubagentStop`), so native subagents are never
hook-nudged. Bounds retained as backstops: a **persistent loop guard** (`nudge-state.json`, scoped
per project+checkpoint, so it survives diff-hash churn across edits — capped at `MAX_NUDGES`, cleared
by a real `mark`, and re-armed at most `MAX_REARMS` times only if genuinely new substantial work
appears since it tripped), read-only seats, per-seat timeouts, hard 2-pass cap.

---

## 9. Explicitly rejected alternatives

Self-assessed confidence as primary gate (conflict of interest); flash-class as everyday baseline
(under-catches); orchestrator-assigned severity (defangs disposition); mid-flow hard-block
(churn); "failing test to reject a finding" (backwards); committing model slugs (goes stale).
