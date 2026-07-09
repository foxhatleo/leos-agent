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
  hook, the skill, and both prompts), a shared council `STATE_ROOT` outside every tool home, an
  in-review marker, and per-seat mechanical isolation (`claude --safe-mode`, isolated `CODEX_HOME`,
  `--agent plan`, `--mode plan`). A council seat can never convene its own council.
- `[all]` **Shared `policy-data.json`** (secret-read denies + fixed-subcommand allowlist) rendered
  per host, with explicit enforced-vs-advisory labels (Codex secret-reads are advisory).
- `[claude] [codex] [opencode] [cursor]` Per-host adapters: enforced hooks/permissions for Claude &
  Codex, an OpenCode guard plugin + permission fragment, a Cursor `beforeShellExecution` shim +
  static deny list.
- `[all]` **Symlink delivery** (`leos-link`) + **fragment merge** (`leos-merge`, ported hardened
  engine with a wider ownership-aware array merge) + **`leos-doctor`** (linkcheck, fragment-drift,
  seat-flag assertions). No ownership-sha, no `MIGRATE`/`RECONCILE`-as-reinstall.
- `[all]` Five test batteries (guard, fmt, council, merge, link) — the actual counts, not "90+".
