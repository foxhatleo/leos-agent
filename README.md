# leos-agent

Leo's unified agent-config for AI coding harnesses — **Claude Code, OpenAI Codex, OpenCode, and
Cursor** — from one repo, one source of truth. It installs a small, shared layer into each tool:

- **A catastrophic-deletion guard** (`bash-guard`) that blocks `rm -rf ~` / `/` / other-users'
  homes / recursive `chmod` of system paths, across every host.
- **A format-on-edit hook** (auto-format + lint feedback for JS/TS, Python, Go, Rust).
- **A multi-model review council**: your host's own model reviews your work alongside other-lineage
  flagships (Opus, GPT, GLM, Gemini, Grok), at plan and implementation checkpoints — with hard
  guarantees that a reviewer can never spin up its own council.
- **A shared permission layer** (secret-read denies + a fixed-subcommand allowlist), rendered into
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

## Upgrade

```sh
git -C ~/.leos-agent pull
```
Because the payload is **symlinked** from each tool home into the clone, `git pull` updates every
hook, the council engine, the skill, and the prompts instantly — no version bumps, no migration.
Then run the doctor:
```sh
python3 ~/.leos-agent/bin/leos-doctor.py
```
If it reports a **fragment changed** (the one thing a symlink can't auto-apply — the few files a
host rewrites itself: `settings.json`, `config.toml`, `opencode.json`, `cli-config.json`), it tells
you the single `leos-merge` command to re-run. Otherwise you're done.

## What lives where

| Path | What |
|---|---|
| `core/` | The single source of truth — guard, formatter, council engine + skill + prompts, shared policy data. Everything here is symlink-target material. |
| `global/AGENTS.md` | Canonical home-dir instructions, symlinked into each host's global path. |
| `tools/<host>/` | Thin per-host adapters: the settings/permission fragment, the symlink map, and setup deltas. No logic or prose forks. |
| `bin/` | `leos-link` (symlink farm), `leos-merge` (fragment merges), `leos-doctor` (health check). |
| `local/` | **Gitignored** machine-local config: your resolved council seats, guard extras, merge state. Lives in the clone but never committed. |
| `tests/` | Guard / formatter / council / merge / link batteries. |

## Uninstall

Remove the symlinks a host owns and restore its config from the backups `leos-merge` wrote to
`local/backups/`. (A dedicated uninstaller may come later; for now the links are visible with
`ls -l` and safe to remove.)

## License

GPL-3.0. See [LICENSE](LICENSE).
