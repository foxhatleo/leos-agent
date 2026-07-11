# Changelog

Human-readable history. **Not** a migration trigger — upgrades are `git pull` + `leos-doctor`
(there is no version/migration system; see `docs/ARCHITECTURE.md`). Entries are tagged by which
host(s) they affect: `[all]`, `[claude]`, `[codex]`, `[opencode]`, `[cursor]`.

## Unreleased — initial build

Merges Leo's previously separate `leos-claude` + `leos-codex` config repos into one, and adds
OpenCode and Cursor support.

### Added
- `[all]` **Hardened `bash-guard`** (the leos-codex lineage) as the baseline, plus a `/home` policy
  fix: the caller's own `$HOME/` subtree is exempt while OTHER users' home trees (`/home/<user>`,
  `/Users/<user>`, any depth) stay critical. 40-case test battery.
- `[all]` **`format-on-edit`** superset (parses both `file_path` and Codex `apply_patch`; realpaths
  HOME). Go/Rust/JS/Python coverage.
- `[all]` **Review council** redesigned around a 5-flagship roster {Opus, GPT, GLM, Gemini, Grok}:
  the host's own model is the native reviewer; the other four (minus the host's provider) are
  external seats. One tool-neutral engine + skill + prompts. Model slugs resolved at setup, never
  committed; the Anthropic seat is always the Opus line (never Fable/Mythos).
- `[all]` **Deterministic anti-recursion**: a `LEOS_COUNCIL_SEAT` env sentinel (checked by the Stop
  hook, the skill, and both prompts), a shared council `STATE_ROOT` (now clone-local under `local/`), an
  atomically owned in-review marker, and per-seat controls (`claude --safe-mode
  --no-session-persistence`, `codex --ephemeral`, `--agent plan`, `--mode plan`). A council seat
  can never convene its own council.
- `[all]` **Shared `policy-data.json`** (secret-read denies + fixed-subcommand allowlist) rendered
  per host, with explicit enforced-vs-advisory labels (Codex secret-reads are advisory).
- `[claude] [codex] [opencode] [cursor]` Per-host adapters: enforced hooks/permissions for Claude &
  Codex, an OpenCode guard plugin + permission fragment, a Cursor `beforeShellExecution` shim +
  static deny list.
- `[all]` **Symlink delivery** (`leos-link`) + **fragment merge** (`leos-merge`, ported hardened
  engine with a wider ownership-aware array merge) + **`leos-doctor`** (linkcheck, fragment-drift,
  seat-flag assertions). No ownership-sha, no `MIGRATE`/`RECONCILE`-as-reinstall.
- `[all]` Nine test batteries (guard, fmt, council, runner, merge, link, block, inject, uninstall).

### Changed (setup & council robustness — from second-machine install feedback)
- `[all]` **Audit remediation.** Fixed arg-transport brace rejection, pushed-feature base collapse,
  documentation/instruction risk filtering, shell control-flow guard bypasses, special untracked
  file reads, vacuous council completion, plan fallback, structured findings validation, and
  critical sign-off. Formatting is now explicit-project-trust-only and never invokes Cargo.
- `[all]` **Ownership-safe lifecycle.** Codex/Cursor hook registries now merge additively; TOML
  merges preserve unrelated comments/order; `leos-seats.py` validates and privately installs
  resolved seat files; `leos-uninstall.py` removes only Leo-owned state and preserves later edits.
- `[all]` **Safer execution policy.** Package scripts and mutating Git commands are no longer
  pre-approved. Runtime health checks validate imports, interpreter identity, and `pip check`.
- `[all]` **Private Python runtime + portable TOML support.** Setup now bootstraps CPython 3.9+
  into `local/.venv`, installs hash-locked `tomli`, and launches every Leo script through
  `bin/leos-python`; no host hook relies on ambient `python3`. GitHub Actions covers Python 3.9,
  3.11, and 3.14 on macOS and Linux.
- `[all]` **Deterministic council CLI runner.** `core/council/bin/runner.py` is explicitly invoked
  by the orchestrator, uses direct argv/process-group timeouts, captures private structured
  results, emits private lifecycle events, and distinguishes completed/empty/missing-content/
  invalid/nonzero/timeout/unavailable states. It blocks nested Leo council runs while allowing
  seats' ordinary subagents; native-only and plan checkpoints retain their intended selection.
- `[all]` **Clone-local runtime state.** Council state, prompts, results, venv/cache, merge/link
  staging, and test scratch all live under `local/`; legacy `~/.local/state` is preserved but no
  longer used by new installs. A source-preserving legacy-state import is explicit-only.
- `[all]` **Host/safety repairs.** OpenCode uses its plural `plugins/` discovery path; Codex links,
  merges, and doctor honor `CODEX_HOME`; `$PWD` and unresolved shell targets are covered by the
  destructive-command guard; link/block/merge writes are backed up, private-staged, and atomic.
- `[all]` **Council seats available via any installed transport, not OpenCode-only.** GLM/Gemini
  (and Opus/GPT) now declare an `asExternalCursor` variant, so `cursor-agent` is a universal
  fallback transport — a Cursor-but-no-OpenCode machine can host the whole council. Setup's rule is
  now "a seat is available if ANY installed transport reaches its provider," not the single catalog
  default.
- `[all]` **Delta-aware council review** (`council.py`): after a checkpoint is reviewed, `mark`
  records a `git write-tree` snapshot of the reviewed worktree; the Stop hook scores only the
  *increment* since that baseline, so a small follow-up fix no longer re-triggers a full review.
  Plus: no-remote/local branches are no longer escalated (diffing against HEAD is legitimate); the
  loop guard is persistent (per project+checkpoint, survives diff-hash churn); a short-TTL risk +
  base cache; `is_small` bar raised (60→120 lines / 3→5 files); per-seat timeout 600→300s.
- `[all]` **Additive, non-clobbering global-instruction delivery** (replaces the whole-file symlink
  that clobbered a user's own global instructions): Claude a managed `@import` block
  (`bin/leos-block.py`), Codex a `SessionStart` injector (`core/hooks/inject-instructions.py`) —
  Codex `@import` is inert and `model_instructions_file` wipes the base prompt, both verified —
  OpenCode an `instructions[]` entry (via a `{{CLONE_ROOT}}` merge token). All stay live on `git
  pull`. `leos-doctor` gained an instruction-delivery + retired-symlink check.
- `[all]` **Setup defaults to the installing agent's own host** — a Claude session sets up `claude`,
  a Codex session `codex`; other detected hosts are offered, not auto-configured.
- `[all]` Test batteries now seven (added `block`, `inject`); council battery rewritten around the
  delta/no-remote/loop-guard/cache cases.

### Fixed (adversarial review remediation)
- `[all]` **Ownership-honest merge state.** The merge snapshot now records only values Leo actually
  introduced: a value the user already had identically is never claimed, so uninstall can no longer
  delete it and a later fragment change conflicts instead of silently "update-owning" it (machines
  merged before this fix keep their old over-claimed snapshots — the pre-merge state is
  unrecoverable — but every re-merge rewrites an honest one). Removal can now retire Leo's leaves
  individually out of a dict the user also populated (previously the whole uninstall refused), a
  fully-Leo dict is still pruned whole, and `--remove` refuses a foreign destination symlink the
  same way merge does instead of replacing the user's symlink with a regular file.
  `leos-uninstall`'s shared-link protection is now evidence-based (registry ∪ seats file ∪ merge
  record ∪ live Leo links) so a lost `installed-hosts.json` entry can't cause deletion of the
  shared `~/.agents/skills/council` while another host still uses it; a hosts-less or non-dict
  registry no longer crashes `leos-link`/`leos-uninstall`.
