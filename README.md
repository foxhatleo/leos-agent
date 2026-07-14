# leos-agent

Portable Claude Code configuration. Clone on any machine (personal or work), run one script, and get the full environment: global instructions, model-routing policy, subagents, skills, hooks, and reusable workflows.

## Install

```sh
git clone git@github.com:foxhatleo/leos-agent.git ~/.leos-agent
~/.leos-agent/install.sh
```

## Update

```sh
git -C ~/.leos-agent pull
```

Everything under `~/.claude` is **symlinked into this repo** (and `CLAUDE.md` is pulled in via `@import`), so a pull is live immediately — no re-install step. Re-run `install.sh` only after a pull that adds a *new* top-level entry (it is idempotent), and use `install.sh check` to detect drift (exit code 1 if any link is broken or replaced).

## Layout

```
install.sh                      idempotent bootstrap / drift check
claude/
  CLAUDE.md                     global directives: model routing, orchestration triggers, cost discipline
  settings.json                 portable Claude Code settings
  agents/
    executor.md                 haiku — mechanical, well-specified grunt work
    investigator.md             sonnet, read-only — research and diagnosis
  skills/                       custom skills (synced to ~/.claude/skills)
  hooks/                        hook scripts referenced from settings.json
  workflows/
    cost-tiered-fix.js          batch-fix workflow: Opus plans/verifies, Haiku/Sonnet execute, escalation on low confidence
```

## Synced vs machine-local

| Synced (this repo) | Machine-local (never committed) |
|---|---|
| `settings.json`, `agents/`, `skills/`, `hooks/`, `workflows/` | `~/.claude/CLAUDE.md` content below the `@import` line |
| Global `CLAUDE.md` (via `@import`) | `~/.claude/settings.local.json`, per-project `.claude/settings.local.json` |
| | Runtime state: `sessions/`, `projects/`, `plans/`, `backups/`, plugin installs |

Secrets and API keys never go in this repo — keep them in environment variables or `settings.local.json` files.

## Using the shared workflows

`~/.claude/workflows` is symlinked to this repo and picked up globally (verified: the workflows appear as invocable by name in any session). In any project, ask Claude to "run the cost-tiered-fix workflow" with a goal or an explicit task list. A project can also carry its own `.claude/workflows/` for repo-specific scripts.

## Model routing (summary)

Canonical version lives in [claude/CLAUDE.md](claude/CLAUDE.md): Opus for investigation/planning/verification, Sonnet as default executor, Haiku for mechanical work; escalate a tier on ambiguity or repeated failure; orchestration fan-out only on explicit trigger phrases ("fan this out", "workflow this", "grind on this", "do this properly").

## Editing flow

Edit files here (directly or through a Claude session — the symlinks mean edits made via `~/.claude` land in the repo too), commit, push. Other machines pick it up on their next `pull`.

**Caveat:** if Claude Code ever rewrites `~/.claude/settings.json` by replacing the file (rather than writing through the symlink), the link breaks silently — `install.sh check` catches this, and re-running `install.sh` repairs it (your changed file is backed up, diff it against the repo copy before discarding).
