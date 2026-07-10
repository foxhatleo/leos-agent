# leos-agent

Leo's unified agent-config for AI coding harnesses — **Claude Code, OpenAI Codex, OpenCode, and
Cursor** — from one repo, one source of truth. It installs a small, shared layer into each tool:

- **A catastrophic-deletion guard** (`bash-guard`) that blocks `rm -rf ~` / `/` / other-users'
  homes / recursive `chmod` of system paths on each host's documented hook/plugin surface. Codex
  interception is necessarily partial for some shell execution paths; it is a safety backstop, not
  a complete command-policy sandbox.
- **A trust-gated format-on-edit hook** (auto-format + lint feedback for JS/TS, Python, Go, Rust
  only after the project root is explicitly listed in gitignored `local/format-trust.json`).
- **A multi-model review council**: your host's own model reviews your work alongside other-lineage
  flagships (Opus, GPT, GLM, Gemini, Grok), at plan and implementation checkpoints. An explicit
  runner records every CLI seat's completed/empty/invalid/error/timeout result and prevents nested
  Leo's Agents councils; seats may still use ordinary subagents.
- **A shared permission layer** (secret-read denies + a read-only fixed-subcommand allowlist), rendered into
  each tool's own enforcement vocabulary.

One `AGENTS.md` serves every tool for instructions; one `SKILL.md` for the council; one
`bash-guard.py` for the guard. Per-tool differences are thin adapter files under `tools/`.

> **Independent from `leos-profiles`.** This repo does not require, assume, or install Leo's shell
> setup (or vice-versa). They pair well; neither depends on the other.

## Install

1. Clone under your home directory:
   ```sh
   git clone git@github.com:foxhatleo/leos-agent.git ~/.leos-agent
   ```
2. Open an AI coding agent **with bypass/full permissions** (Claude Code
   `--dangerously-skip-permissions`, Codex `--dangerously-bypass-approvals-and-sandbox`, or OpenCode
   in bypass mode) and point it at this repo's runbook:
   > Set up my agent config by following `~/.leos-agent/AGENTS.md` for this machine.
3. The agent runs the interview in [`docs/SETUP.md`](docs/SETUP.md): **by default it sets up only
   the host you asked from** (the agent you're talking to configures its own environment) and offers
   to do the others on request. For each host it creates the symlinks, merges the settings
   fragments, adds the global-instruction reference (so your own instructions are never clobbered),
   and — for the council — asks which reviewer transports you want and resolves each model's current
   flagship slug (stored machine-locally, never committed). Model versions are **never hard-coded**;
   today's Opus/GPT are already about to be superseded.

Nothing is installed until you run setup — cloning alone is inert.

Setup first creates `local/.venv` from an approved CPython 3.9+ bootstrap interpreter and installs
the pinned runtime dependency there. Leo scripts always use `bin/leos-python`, never an ambient
host-hook `python3`.

## Upgrade

```sh
git -C ~/.leos-agent pull
```
Because executable payloads are symlinked from each tool home into the clone, `git pull` updates
them instantly. Host-owned JSON/TOML files—including Codex/Cursor hook registries—are additive
ownership-tracked merges and must be refreshed after a fragment changes. Refresh the runtime,
reapply the configured-host merges reported by doctor, then check again:
```sh
python3 ~/.leos-agent/bin/leos-runtime.py setup --refresh
~/.leos-agent/bin/leos-python ~/.leos-agent/bin/leos-doctor.py
```
Doctor reports a changed merge fragment, a stale private runtime, or host links that need attention.
It only checks hosts recorded during Leo setup, not every config directory that happens to exist.

## What lives where

| Path | What |
|---|---|
| `core/` | The single source of truth — guard, formatter, council engine + skill + prompts, shared policy data. Everything here is symlink-target material. |
| `global/AGENTS.md` | Canonical instructions, delivered additively where a host supports it; Cursor remains per-project. |
| `tools/<host>/` | Thin per-host adapters: the settings/permission fragment, the symlink map, and setup deltas. No logic or prose forks. |
| `bin/` | Private runtime/launcher, link/merge/block tools, validated seat writer, ownership-safe uninstaller, policy renderer, and doctor. |
| `local/` | **Gitignored** machine-local config and all Leo runtime data: venv, resolved seats, guard extras, merge state/backups, council state/work/results. |
| `tests/` | Guard / formatter / council / runner / merge / link / block / inject / uninstall batteries. |

## Uninstall

Run the ownership-safe uninstaller for each configured host:

```sh
bin/leos-python bin/leos-uninstall.py --tool <claude|codex|opencode|cursor>
```

It removes only Leo-owned links, merge values, and managed instruction blocks; later user edits
are preserved. Whole-file backups under `local/backups/` are disaster recovery, not uninstall state.

## License

GPL-3.0. See [LICENSE](LICENSE).
