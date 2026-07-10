# Architecture & rationale

Why leos-agent is shaped the way it is. (Committed on purpose — the design-of-record must not live
only in a gitignored scratch dir.)

## Why one repo

Leo previously ran two near-identical config repos (`leos-claude`, `leos-codex`) with no shared
source of truth. A hardening round reached one and not the other at the same version — a silent
security regression (the weaker guard was the one installed). A single `core/` makes that class of
drift **physically impossible**: there is one `bash-guard.py`, one council engine, one skill, one
policy dataset. Four harnesses now converge on shared standards (`AGENTS.md` instructions, the
`SKILL.md` Agent-Skills format, PreToolUse-style command hooks), so the per-host surface is thin
adapters, not forks.

## Maximum sharing

- **Instructions:** one canonical `global/AGENTS.md`, delivered to each host by an **additive
  reference** — never a whole-file symlink, which would clobber a user's own global instructions:
  - **Claude** — a managed `@import` block in `~/.claude/CLAUDE.md` (Claude resolves `@import`
    natively; coexists with the user's content; live on pull). Written by `bin/leos-block.py`.
  - **Codex** — a `SessionStart` hook that injects `global/AGENTS.md` as `additionalContext`.
    (Empirically verified: Codex `@import`/`@./path` is inert, and `model_instructions_file`
    *replaces* the base/system prompt — both unusable; the hook is additive and base-safe.)
  - **OpenCode** — an `instructions[]` entry in the merged `opencode.json` (absolute clone path via
    the `{{CLONE_ROOT}}` merge token; additive with the user's `AGENTS.md`).
  - **Cursor** — no global file; per-project `AGENTS.md` only.
- **Council:** one `SKILL.md` in `~/.agents/skills` (read natively by Codex, OpenCode, Cursor) +
  `~/.claude/skills/council` for Claude. One engine, data-driven from `local/seats.<host>.json`.
- **Guard:** one `bash-guard.py` core + a thin per-host registration (Claude/Codex PreToolUse
  exit-2, Cursor `beforeShellExecution` shim, OpenCode plugin).
- **Policy:** one `policy-data.json`, rendered per host, with enforced-vs-advisory labels so the
  boundary is explicit and never silently crossed.

## Delivery: hybrid symlink

Symlinks retire the copy/ownership/migration machinery for everything a host does NOT rewrite. The
gains and the guardrails:

- **`git pull` = upgrade.** The symlinked payload updates live. No version numbers, no `MIGRATE`.
- **Self-location.** Hooks/engine find `<clone>/local/` via `realpath(__file__)`, so one symlinked
  script serves any tool home.
- **Everything machine-local lives in the clone, gitignored** (`local/`) — including the council
  seats. This is Leo's explicit choice: config is asked at setup and saved in the clone (never in a
  tool home, never committed), which also sidesteps the "don't symlink a machine-local file into the
  repo" hazard.
- **Merge survives for the 4 host-rewritten files** (`settings.json`, `config.toml`, `opencode.json`,
  `cli-config.json`). `leos-merge` snapshots first, refuses on conflict, and records a fragment hash
  so `leos-doctor` can flag drift — the only thing `git pull` can't auto-apply.
- **Fail-closed guard wrapper.** A dangling guard symlink exits 2 (blocks) rather than silently
  passing; the formatter/council hooks fail open (availability over safety, since they aren't
  tripwires).
- **Release worktree (optional, hardened).** Symlink from a `git worktree` pinned to a release tag,
  not the dev clone, so a `git checkout`/rebase in dev can't silently downgrade live hooks.

## Council: host = native, roster minus host = external

The five flagship roles are fixed; the exact model slug is resolved at setup, never committed (they
go stale — Opus 4.8 / GPT-5.5 are already about to be superseded). The host's own model is the
native reviewer; the other four flagships (minus the host's provider) are external. The Anthropic
seat is always the **Opus line** — never Fable or Mythos.

### Anti-recursion (a seat never convenes its own council)

Deterministic first, tool-agnostic:

1. `LEOS_COUNCIL_SEAT=1` env sentinel on every external-seat launch (inherited by child hooks).
2. The Stop hook returns 0 immediately when the sentinel is set.
3. The skill's first paragraph and both review prompts check the sentinel.
4. One shared `STATE_ROOT` outside every tool home + an in-review marker cover env-stripping CLIs
   and cross-tool visibility.
5. Per-seat mechanical isolation (`claude --safe-mode` is the only true one; others use read-only +
   dir/env hygiene).

Only the `Stop` event is registered (never `SubagentStop`), so native subagents are never
hook-nudged. Backstops retained: 2-nudge loop guard, read-only seats, per-seat timeouts, 2-pass cap.

## Honest boundaries (do not paper over)

- **Codex secret-reads are advisory**, not pattern-enforced (no declarative read-deny surface);
  coverage is hook/sandbox-mediated. The policy renderer labels this; a CI contamination check
  asserts Codex/Cursor config never contains a literal Claude permission string.
- **Cursor CLI headless hooks/skills are UNCERTAIN** across versions. The static `Shell(...)`/`Read(...)`
  deny list in `cli-config.json` is Cursor's reliable surface; the `beforeShellExecution` shim is
  defence-in-depth. Verify with the smoke test before claiming the hook is active.
- **OpenCode has no Stop-event hook**, so there is no automatic council nudge there — the council
  runs via the skill + the global `AGENTS.md` mandate.
- **Model slugs and Cursor's Grok slug** must be resolved at setup (`cursor-agent --list-models`);
  nothing here pins them.

## What deliberately does not exist

A `VERSION` file as a migration trigger, `MIGRATE.md`, `RECONCILE`-as-reinstall, ownership-sha
hashing, a copy-based installer, and per-tool prose forks. `CHANGELOG.md` is human history only.
