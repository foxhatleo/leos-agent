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
3. **No secrets in the repo.** `local/` is gitignored and holds all Leo-owned machine-local config
   and runtime material (resolved council seats, guard extras, venv, state, work/output, merge
   state/backups). Never commit anything under `local/`. Never write a token/key into a tracked file.
4. **Resolve model slugs at setup, never commit them.** The council roster (`core/council/seats.catalog.json`)
   uses `{MODEL}` placeholders. You resolve each provider's CURRENT flagship at setup and write the
   concrete slug into a gitignored candidate and install it with `bin/leos-seats.py`. **For OpenAI
   (Leo's standing rule): use the most capable flavor of the newest GPT generation. GPT-5.6 ships
   three capability flavors — Sol > Terre > Luna (new names in 5.6, not lineages) — so 5.6
   resolves to Sol, never Terre or Luna; a newer GPT generation supersedes 5.6 automatically and
   its most capable flavor is selected.** The Anthropic seat is always the Opus line — never Fable
   or Mythos.

## The model

- **Delivery = executable symlinks + ownership merges.** Scripts, council engine, skill, prompts,
  and launcher are linked. Host registries/config—including Codex/Cursor `hooks.json`—are merged
  additively so user entries coexist and uninstall removes only values Leo still owns.
- **Self-location.** The hook scripts and council engine find their machine-local config in
  `<clone>/local/` via `realpath(__file__)`, so they work through the symlink from any tool home.
- **Private runtime.** Bootstrap an approved CPython 3.9+ into `local/.venv` with
  `python3 bin/leos-runtime.py setup`; all normal commands use `bin/leos-python`, never bare
  `python3`. The hash-locked external TOML dependency lives only in that venv.
- **Upgrade checks.** Upgrade = `git pull` + `python3 bin/leos-runtime.py setup --refresh` +
  `bin/leos-python bin/leos-doctor.py`. Doctor detects a changed runtime lock or merge fragment
  and prints the corrective command.

## Do it

Run the interview in [`docs/SETUP.md`](docs/SETUP.md) top to bottom. Create the private runtime
before any link/merge operation. It detects the installed hosts
(Claude Code / Codex / OpenCode / Cursor) but **by default configures only your own host** — the
one you (the installing agent) are running on (Claude→`claude`, Codex→`codex`, etc.); the other
detected hosts are offered, not auto-configured, and only added when Leo explicitly asks. For each
host you configure: `leos-link` → `leos-merge` → `leos-block` (Claude only) → validated
`leos-seats.py write` after driver smokes → verify. The
per-host specifics live in `tools/<host>/SETUP-DELTA.md`. Before declaring done, run **all test
batteries** (`tests/{guard,fmt,council,runner,merge,link,block,inject,uninstall,runtime,seats}-tests.py`, contamination,
and policy checks) and `bin/leos-doctor.py`
— all must pass.

## Never

- Never paste Claude `permissions.allow`/`deny` strings into Codex or Cursor config — the
  vocabularies differ and Codex's secret-read coverage is advisory (see `core/policy/policy-data.json`).
- Never add a council seat before its driver smoke test passes (`core/council/drivers/`).
- Never commit `local/`; never hard-code a model slug in a tracked file.
- Never invoke a council from a council seat. Ordinary seat subagents are allowed; nested Leo's
  Agents councils are not. Use `core/council/bin/runner.py` only when the orchestrator explicitly
  decides to run a review.
