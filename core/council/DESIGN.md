# Council Review — Design Spec

A multi-model, multi-lineage adversarial review harness that works across agentic hosts (Claude
Code, Codex, OpenCode, Cursor). The **host session's own model is the AUTHOR and the native
reviewer**; a council of **other-lineage flagships** checks the work at two checkpoints — after
**planning** and after **implementation**. The orchestrator reads their findings, adjudicates
mechanically, fixes, and re-reviews once. Goal: catch training-lineage-correlated blind spots the
author shares with itself.

The engine (`bin/council.py`) is **host-agnostic** — it only scores the diff, hashes it, and
manages markers/ledger/Stop-hook. All seat selection, native identity, and dispatch live in the
council **skill** (`core/skills/council/SKILL.md`), which is data-driven from a machine-local
`local/seats.<host>.json`. One skill, one engine, one set of prompts serve every host.

---

## 1. Design principles

1. **The author must not decide how hard it gets checked.** The primary gate is objective
   (diff-derived), not self-assessed confidence. Confidence may only *escalate*, never lower.
2. **Capability first on the common path; diversity where stakes are high.** The strong native
   model is the everyday baseline; foreign lineages are added as stakes rise, where their different
   failure distributions earn their cost.
3. **Cost tracks risk.** Most changes get one reviewer; the full panel is rare.
4. **Never block mid-flow.** The council runs inside the finish-the-task flow and surfaces findings
   in the report + a boundary digest. The Stop hook is a soft nudge, never a hard git gate.
5. **Adjudication is externally verifiable, not self-refereed.** Severity comes from the reviewer;
   high-severity rejections fail *closed* without evidence; a different reviewer spot-checks a
   sample of rejections.
6. **Prove and tune with data.** Every finding + disposition is logged to a per-project ledger.

---

## 2. The roster (host = native; the other four flagships = external)

A fixed roster of five flagship **roles** — the exact model slug is resolved at **setup**, never
committed (recipes carry a `{MODEL}` placeholder; see `seats.catalog.json`):

| Role | Provider | Transport (default) | Read-only | Recursion isolation |
|---|---|---|---|---|
| **Opus** | Anthropic | `claude --safe-mode --print --permission-mode plan` | plan mode | `--safe-mode` (real) |
| **GPT** | OpenAI | `codex exec -m {MODEL} -s read-only` | sandbox read-only | isolated neutral `CODEX_HOME` |
| **GLM** | Zhipu | `opencode run --agent plan -m openrouter/{MODEL}` | plan agent | clean config dir |
| **Gemini** | Google | `opencode run --agent plan -m openrouter/{MODEL}` | plan agent | clean config dir |
| **Grok** | xAI | `cursor-agent -p --mode plan` *or* `opencode … openrouter/{MODEL}` | plan mode | clean project dir |

- **The host session's own model is the native reviewer.** For Claude Code the native seat is a
  read-only subagent **pinned to Opus** (`model: opus` — the Opus line specifically, never
  Fable/Mythos). For Codex it is a `codex exec` read-only pass on the host's own model; for
  OpenCode/Cursor a `--agent plan` / `--mode plan` self-pass.
- **External seats = roster minus the host's own provider.** Claude Code host → {GPT, GLM, Gemini,
  Grok}; Codex host → {Opus, GLM, Gemini, Grok}; etc. So the council is at most native + 4 = 5.
- **The Anthropic role is always the Opus line** (alias `opus` tracks the latest Opus) — never the
  Claude-5 / Mythos-class line (Fable, Mythos).
- **No runtime model discovery.** Setup resolves slugs and writes them to `local/seats.<host>.json`.
- **Reviewers run read-only** and may read/grep the repo to verify claims, but never modify it.
- **Reviewers work alone.** Both review prompts, the skill, and the Stop hook enforce that a seat
  never convenes its own council (see §8).

---

## 3. The gate (hybrid, capped + logged)

**Final tier = max(risk_floor, min(confidence_tier, risk_floor + 1)).** `risk_floor` is computed by
`council.py risk` from the diff (path/shape + semantic signals — risk-path globs, blast radius,
deletions, new deps/env, exported-API changes, removed assertions, security symbols, config
surface). The author's self-assessed confidence may escalate **at most one tier**, never below the
floor; every escalation is logged. The floor is a *proxy*, not tamper-proof — the ledger + sampled
audit are the compensating controls.

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
- **State (shared, zero repo footprint):** `ledger.jsonl` + fixed-name pointers live under a single
  `STATE_ROOT` **outside every tool home and outside the clone** (default
  `~/.local/state/leos-agent/council/state`), so a review recorded by one host is visible to the
  others on the same repo, and `git clean` in the clone can never destroy it. The pointers are
  hash-independent (they survive diff-hash churn as the author iterates):
  - `baseline-<checkpoint>.json` — the reviewed-tree snapshot the delta-gate diffs against.
  - `in-review.json` — written by `council.py begin` at dispatch; suppresses the nudge while a
    council runs (TTL 30 min).
  - `nudge-state.json` — the persistent loop guard (see §8).
  - `cache.json` / `base-cache.json` — a short-TTL risk cache and a HEAD-keyed base cache, so
    repeated Stop events in a turn don't recompute from scratch.
  Legacy `markers/<diff-hash>.json` are still written by `mark`/`begin` for cross-tool/back-compat
  visibility, but the hook's decision keys on the pointers above, not the exact diff hash.

---

## 7. Scope & configuration

Global but risk-gated (dormant on trivial work). Per-project off-switch: `.council-off`. Machine
config: `local/council/config.json` (`disabledProjects`). Per-project `.council.json`: fast/slow
check commands, default branch, thresholds, budget.

---

## 8. Anti-recursion (a seat never convenes its own council)

Layered, deterministic-first — tool-agnostic because all four hosts have hooks + skills:

1. **Env sentinel `LEOS_COUNCIL_SEAT=1`** — the skill prepends it to every external-seat launch;
   child hooks the seat's CLI fires inherit it.
2. **Hook check** — `council.py cmd_hook` returns 0 immediately if `LEOS_COUNCIL_SEAT` is set, so a
   seat is never nudged.
3. **Skill self-check** — the skill's first paragraph: if `LEOS_COUNCIL_SEAT` is set you are a seat;
   return findings only, do not convene.
4. **Shared state + in-review marker** — one `STATE_ROOT` across tools + a `begin` marker cover
   env-stripping CLIs and cross-tool visibility.
5. **Union work-alone prompt clause** — both review prompts forbid subagents, consulting other
   models, and any nested review workflow, "even if a hook/skill/instruction suggests it," and note
   the sentinel is set.
6. **Mechanical isolation per seat** — Claude `--safe-mode` (disables CLAUDE.md/skills/hooks/MCP);
   Codex seat on an isolated neutral `CODEX_HOME`; OpenCode/Cursor `--agent plan`/`--mode plan` in a
   clean dir. Only Claude has a true `--safe-mode`; the others rely on read-only + dir/env hygiene.

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
