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
- `[all]` **Pre-approved command surface is now actually read-only.** `git branch` left the
  universal allow set (its mutating forms `-d/-D/-m/-f/--delete` share the prefix with the
  read-only listing and the hosts' prefix-based allow vocabularies cannot exclude flags) — hosts
  prompt for it again. As defense in depth, `bash-guard` now blocks the write primitives those
  vocabularies cannot express: `git diff/log/show --output=<file>` and the mutating `git branch`
  flags, including behind wrappers and `git -C` global options. The guard's `$HOME`/`$PWD`
  expansion is boundary-aware (`$HOME_old`/`$PWD_old` stay conservatively unknown instead of
  mis-expanding to safe-looking home paths), and a `pwd`-module shadowing bug that made `~user`
  expansion crash fail-open (silently allowing `rm -rf ~root/…`) is fixed. Installed machines see
  a one-time doctor fragment-drift notice per host — re-run `leos-merge --tool <host>` to retire
  the old `git branch` allow entry.
- `[all]` **Runtime lifecycle honesty.** An explicitly requested bootstrap interpreter
  (`--python`/`LEOS_PYTHON`) that is missing or older than 3.9 is now a hard error naming the
  request instead of a silent fallback to an ambient `python3`; `setup` exits nonzero when the
  freshly swapped runtime fails its own health probe; the venv stage+swap is serialized by
  `local/runtime.lock` and followed by a path fixup (re-run `venv` + shebang rewrite) so no
  `.venv-staging-*` path survives in `pyvenv.cfg`/`activate`/console scripts; and
  `bin/leos-python` honors `LEOS_LOCAL` like `leos-runtime`/`leos-doctor` already did, so the
  runtime setup builds and doctor validates is the one the launcher runs. New `runtime` test
  battery (wired into CI, SETUP, and the runbook battery list).
- `[all]` **Seat validation matches the documented templates and enforces the model rules.**
  `{EFFORT}` is a runtime placeholder the runner substitutes per review tier — doctor no longer
  flags it as unresolved, so the exact rosters the catalog and SETUP.md prescribe now pass
  `leos-seats.py validate`/`write` (previously every documented candidate was refused). The
  Anthropic-seat rule is mechanical instead of prose: a Claude host's native subagent model must
  be Opus-line (never Fable/Mythos), a literal `{MODEL}` is refused anywhere including the native
  subagent model, and an external `opus` seat must pin `--model` to an Opus-line id (an unpinned
  seat previously passed silently and ran the CLI's default model). Machines with an unpinned or
  non-Opus seat will newly fail doctor — the message says what to pin. New `seats` conformance +
  rejection battery (wired into CI, SETUP, and the runbook battery list).
- `[all]` **Runner cancellation is race-free, bounded, and exactly classified.** Seat launch and
  registration now happen atomically against the cancel handler (a signal in the old
  post-`Popen`/pre-registration window left an unkillable seat running to its full timeout —
  empirically 9/9 at 0.15–0.35 s; now a bounded ~0.6 s teardown), `seat-started` is emitted only
  once the child is killable, and a cancelled run SIGTERMs then SIGKILLs within a 5 s grace.
  Classification is exact: `cancelled` only under real cancellation, a seat that finished before
  the signal stays `completed`, and an externally signalled seat is the new `signal-exit`.
  Lifecycle stderr writes, the final summary print, and the interpreter's shutdown flush all
  survive a dead orchestrator pipe (`result.json` is authoritative; no more exit-120). The unused
  `LEOS_COUNCIL_ACTIVE_RUN` export to seats (the `--run-id` ownership token) is gone. The
  cancellation test waits on lifecycle events instead of a fixed sleep, and a hung runner is a
  failing check with diagnostics instead of a battery abort.
- `[all]` **Dispatch safety: adapter allow-list, typed plan fallback, real scratch-cwd isolation.**
  Seat adapters are allow-listed (`claude`/`codex`/`opencode`/`cursor-json`/explicit `raw`): an
  unknown binary is `invalid-seat-config` instead of silently inferred `raw`, unrecognized
  structured output can never classify `completed` (`unsupported-adapter`), and external seat
  names are path-safe-validated. A plan review whose externals all fail no longer crashes on a
  missing/invalid native block — it records a typed fallback error, writes `result.json`, and
  releases the marker instead of leaking it for the full TTL. CLI seats now actually launch in a
  per-seat empty scratch directory under `local/council/work` (removed after the seat) with the
  reviewed repo's absolute path injected as a prompt header — making the catalog/driver "clean
  dir" isolation real, so repo-local agent config (`.cursor/rules`, `AGENTS.md`) no longer loads
  into reviewers; a per-seat `"cwd": "repo"` opt-out carries the documented residual risk. The
  skill's status taxonomy now lists every terminal state the runner produces.
- `[all]` **Fix→re-review is a first-class second pass.** `runner.py run --follow-up
  [--seat <name>]` reuses the active run's marker and run id for the mandated single-seat
  re-review, writes under `<run>/pass-2/` with round-1 artifacts immutable, refuses a third pass,
  and a finished `--run-id` can no longer be silently overwritten without it
  (`run-id-work-exists` — previously reuse clobbered `result.json`/`job.json` and interleaved the
  event log). `council.py mark` gained an opt-in `--run-id` ownership check (exit 3
  `active-run-not-owned`) so an orchestrator that knows its run id can never close another run's
  fresh marker; plain `mark` keeps the legacy checkpoint-scoped clearing for manual and Stop-hook
  override flows. The skill documents both commands.
- `[all]` **Critical-tier close path documented end-to-end.** The council skill's close command
  and the Stop-hook override text now include the `--signoff` requirement `mark` already enforces
  at the critical tier, so a critical review can actually be closed as documented (the printed
  override command previously always failed for critical-scoring diffs).
- `[all]` **OpenAI council-seat rule clarified as generation-scoped (Leo's standing rule).**
  GPT-5.6 introduced three capability flavors — Sol > Terre > Luna, new names in 5.6, not
  lineages — and the rule's intent is "within a generation, pick the most capable flavor" (5.6 →
  Sol, never Terre/Luna), with a newer GPT generation superseding automatically. The earlier
  wording (an undocumented commit) read as version entrenchment — "floor and preference", "only a
  higher numeric version supersedes", "never a same-version sibling" — which would have frozen
  Sol against successors and never fired if OpenAI dropped numeric names. Reworded across the
  runbook, README, catalog, drivers, and setup docs; stale "native seat = the host's own model"
  statements reconciled with the Codex-native pin (native-only fallback mode genuinely runs the
  session's own model — now stated explicitly); the README's "model versions are never
  hard-coded" principle restored to full strength.
- `[all]` **Close three residual post-review gaps.** Council scratch directories now establish a
  synthetic Git project root, so parent-repo instructions cannot regain authority when this clone
  reviews itself while all state/work remains under `local/`; dispatch fails typed
  `isolation-error` if the boundary cannot be created. Runtime upgrades retain the prior venv and
  exact runtime-state bytes until the replacement passes its final health probe, rolling both back
  on any post-swap failure instead of returning with a broken replacement. Anthropic validation
  now keys policy to a required external-seat `provider` identity (with the `claude` transport as
  defense-in-depth) rather than the freely chosen seat display name, including Cursor/OpenRouter
  fallback transports. The Opus-id recognizer accepts only the `opus` alias or a Claude-namespaced
  Opus slug—not arbitrary strings that merely contain `opus`. Existing machine-local rosters that
  predate the `provider` field must be regenerated through SETUP step 5 and reinstalled with
  `leos-seats.py`; doctor refuses them rather than guessing identity from a display name.
