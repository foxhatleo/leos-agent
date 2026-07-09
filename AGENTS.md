# leos-agent — setup & maintenance runbook (for the installing agent)

You are an AI coding agent asked to install or upgrade Leo's unified agent config on THIS machine.
This file is the runbook; the full step-by-step interview is [`docs/SETUP.md`](docs/SETUP.md). The
architecture rationale is [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). (This is a setup-time
document — it is addressed to you, not runtime behavioral instructions for coding sessions. Those
live in [`global/AGENTS.md`](global/AGENTS.md).)

## Prime directives

1. **ASK when unsure.** Never guess a destructive or machine-specific choice. On any conflict the
   tools REFUSE and report — resolve with Leo, then retry. Do not force past a refusal blindly.
2. **Never clobber; back up first.** All merges into host-owned files go through `bin/leos-merge.py`
   (it snapshots the dest first). All symlinks go through `bin/leos-link.py` (it refuses to replace
   a foreign regular file without `--force`). Never hand-edit a host config when a tool can do it.
3. **No secrets in the repo.** `local/` is gitignored and holds all machine-local config (resolved
   council seats, guard extras, merge state). Never commit anything under `local/`. Never write a
   token/key into a tracked file.
4. **Resolve model slugs at setup, never commit them.** The council roster (`core/council/seats.catalog.json`)
   uses `{MODEL}` placeholders. You resolve each provider's CURRENT flagship at setup and write the
   concrete slug into `local/seats.<host>.json`. **The Anthropic seat is always the Opus line — never
   Fable or Mythos.**

## The model

- **Delivery = symlinks + a few merges.** Hooks, the council engine, the skill, and prompts are
  symlinked from each tool home into this clone (so `git pull` upgrades them live). The handful of
  files a host rewrites itself (`settings.json`, `config.toml`, `opencode.json`, `cli-config.json`)
  are merged, not linked.
- **Self-location.** The hook scripts and council engine find their machine-local config in
  `<clone>/local/` via `realpath(__file__)`, so they work through the symlink from any tool home.
- **No version/migration system.** Upgrade = `git pull` + `bin/leos-doctor.py`. Doctor detects the
  only thing a pull can't auto-apply: a changed merge fragment (it prints the one `leos-merge`
  command to re-run).

## Do it

Run the interview in [`docs/SETUP.md`](docs/SETUP.md) top to bottom. It detects the installed hosts
(Claude Code / Codex / OpenCode / Cursor), then for each: `leos-link` → `leos-merge` → write
`local/seats.<host>.json` (asking the council transport questions + resolving slugs) → verify. The
per-host specifics live in `tools/<host>/SETUP-DELTA.md`. Before declaring done, run **all five test
batteries** (`tests/{guard,fmt,council,merge,link}-tests.py`) and `bin/leos-doctor.py` — all must
pass.

## Never

- Never paste Claude `permissions.allow`/`deny` strings into Codex or Cursor config — the
  vocabularies differ and Codex's secret-read coverage is advisory (see `core/policy/policy-data.json`).
- Never add a council seat before its driver smoke test passes (`core/council/drivers/`).
- Never commit `local/`; never hard-code a model slug in a tracked file.
