# leos-agent

Portable Claude Code configuration. Clone on any machine (personal or work), run one script, and get the full environment: global instructions, cost-tiered model routing, subagents, skills, and reusable workflows.

## Install

```sh
git clone git@github.com:foxhatleo/leos-agent.git ~/.leos-agent
~/.leos-agent/install.sh
~/.leos-agent/install.sh mcp   # optional: register MCP servers (see below)
```

## Update

```sh
git -C ~/.leos-agent pull
```

The synced entries under `~/.claude` are **symlinked into this repo** (and `CLAUDE.md` is pulled in via `@import`), so a pull is live immediately — no re-install step. Re-run `install.sh` only after a pull that adds a *new* top-level entry (it is idempotent); re-run `install.sh mcp` after a pull that changes `claude/mcp.list`. `install.sh check` reports drift and exits 1.

## Layout

```
install.sh                      idempotent bootstrap / drift check / mcp registration
claude/
  CLAUDE.md                     global directives: model routing, execute-then-review,
                                delegation rules, orchestration triggers, ticket sources
  settings.json                 portable Claude Code settings
  mcp.list                      portable MCP server manifest (install.sh mcp)
  agents/
    explore.md                  haiku override of the built-in Explore scout
    executor.md                 haiku — mechanical, well-specified grunt work
    implementer.md              sonnet — executes an approved plan
    investigator.md             opus, read-only — root-cause diagnosis
    reviewer.md                 opus, read-only — diff verdicts, confidence-gated findings
  skills/
    review-pr/                  /review-pr — stage a pending GitHub PR review + verdict
    fix/                        /fix — ticket to draft PR with a sign-off gate
  hooks/                        hook scripts referenced from settings.json
  workflows/
    cost-tiered-fix.js          batch-fix workflow: Opus plans/verifies, Haiku/Sonnet execute
```

## Model routing (summary)

Canonical version lives in [claude/CLAUDE.md](claude/CLAUDE.md): Opus for investigation/planning/review, Sonnet as default executor, Haiku for mechanical work; **every execute request ends with an Opus review of the diff before it is called done**; escalate a tier on ambiguity or two failures; multi-agent fan-out only on explicit trigger phrases ("fan this out", "workflow this", "grind on this", "do this properly").

## Skills

| Skill | Trigger | What it does |
|---|---|---|
| `/review-pr [n]` | "review PR 42" | Sonnet lens agents read the diff, the Opus main loop adjudicates, and inline comments are **staged as a PENDING review on GitHub** (never submitted — only you see them). Chat report gives a ready / neutral / seriously-problematic verdict. |
| `/fix [ticket-id]` | "fix ENG-123" | Resolves the ticket from Linear or Jira (first unknown prefix: asks once, persists the mapping to CLAUDE.md), pulls linked Confluence/Slack/GitHub context, investigates and plans at Opus, **waits for explicit sign-off**, implements on a worktree branch with Haiku/Sonnet executors, Opus-reviews the diff, then pushes and opens a draft PR in the browser. |

## Synced vs machine-local

| Synced (this repo) | Machine-local (never committed) |
|---|---|
| `settings.json`, `agents/`, `skills/`, `hooks/`, `workflows/`, `mcp.list` | `~/.claude/CLAUDE.md` content below the `@import` line |
| Global `CLAUDE.md` (via `@import`) | `settings.local.json` files; MCP registrations + OAuth state in `~/.claude.json` |
| | Runtime state: `sessions/`, `projects/`, `plans/`, `backups/`, plugin installs |

Secrets and API keys never go in this repo — keep them in environment variables or `settings.local.json` files.

## MCP servers

`install.sh mcp` registers every server in [claude/mcp.list](claude/mcp.list) at user scope (skipping ones already present). Registration is deliberately opt-in — work machines may not want personal servers. Two caveats:

- **OAuth (Linear, Atlassian)**: registration ≠ authentication. Run `/mcp` inside a session once per machine to complete the browser OAuth flow.
- **Slack**: Slack's hosted MCP doesn't support the standard OAuth flow Claude Code uses (no dynamic client registration). One-time setup: create a Slack app for your workspace, complete its OAuth manually to mint a token, and export it as `SLACK_MCP_TOKEN` before starting Claude Code (e.g. in your shell profile). The manifest passes it as a bearer header, unexpanded, so the token itself is never written to disk by this repo.

## Using the shared workflows

`~/.claude/workflows` is symlinked to this repo and picked up globally (verified: the workflows appear as invocable by name in any session). In any project, ask Claude to "run the cost-tiered-fix workflow" with a goal or an explicit task list. A project can also carry its own `.claude/workflows/` for repo-specific scripts.

## Editing flow

Edit files here (directly or through a Claude session — the symlinks mean edits made via `~/.claude` land in the repo too), commit, push. Other machines pick it up on their next `pull`. Note `/fix` appends ticket-prefix mappings to `claude/CLAUDE.md` on first encounter — commit those when they show up.

**Caveat:** if Claude Code ever rewrites `~/.claude/settings.json` by replacing the file (rather than writing through the symlink), the link breaks silently — `install.sh check` catches this, and re-running `install.sh` repairs it (your changed file is backed up, diff it against the repo copy before discarding).
