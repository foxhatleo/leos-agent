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

Content directories under `~/.claude` are **symlinked into this repo** (and `CLAUDE.md` is pulled in via `@import`), so a pull is live immediately for agents, skills, hooks, and workflows. Re-run `install.sh` after a pull that changes `claude/settings.json` (it's populated, not linked) or adds a new top-level entry; re-run `install.sh mcp` after a pull that changes `claude/mcp.list`. `install.sh check` reports drift and exits 1.

## Wiring principles

- **Config files**: an **import directive** where the format supports it (`CLAUDE.md` via `@import`); **populated** — written as real, merged files — where it doesn't (`settings.json`). Populating is the fallback, not the preference.
- **Symlinks are only for additions**: whole content directories (`agents/`, `skills/`, `hooks/`, `workflows/`) where the repo owns everything inside.
- **`$LEOS_AGENT_PATH`** is an *optional* override, defaulting to `~/.leos-agent`. Nothing sets or exports it — everything reads `${LEOS_AGENT_PATH:-$HOME/.leos-agent}` at use time. Export it (shell profile) only if you cloned the repo somewhere else.
- **Machine-local state**: anything a skill or agent persists is JSON in `local/<name>.json`, keyed per `owner/repo` — written via `claude/scripts/state.py`, gitignored, never synced.

## Layout

```
install.sh                      idempotent bootstrap / drift check / mcp registration
local/                          machine-local skill/agent state (gitignored, per-repo JSON)
claude/
  CLAUDE.md                     global directives: model routing, execute-then-review,
                                delegation rules, orchestration triggers, machine-local state
  settings.json                 portable Claude Code settings (merged into ~/.claude/settings.json)
  mcp.list                      portable MCP server manifest (install.sh mcp)
  scripts/
    state.py                    shared get/merge helper for local/ state files
  agents/
    explore.md                  haiku override of the built-in Explore scout
    executor.md                 haiku — mechanical, well-specified grunt work
    implementer.md              sonnet — executes an approved plan
    investigator.md             opus, read-only — root-cause diagnosis
    reviewer.md                 opus, read-only — diff verdicts, confidence-gated findings
    expert.md                   fable, read-only — ceiling tier: hardest verdicts and
                                arbitration only, manual triggers or rare auto-escalation
  skills/
    review-pr/                  /review-pr — stage a pending GitHub PR review + verdict
    resolve-ticket/             /resolve-ticket — ticket to draft PR with a sign-off gate
    watch-review/               /watch-review — poll for direct review requests (via /loop)
  hooks/                        hook scripts referenced from settings.json
  workflows/
    cost-tiered-fix.js          batch-fix workflow: Opus plans/verifies, Haiku/Sonnet execute
```

## Model routing (summary)

Canonical version lives in [claude/CLAUDE.md](claude/CLAUDE.md): Opus for investigation/planning/review, Sonnet as default executor, Haiku for mechanical work; **every execute request ends with an Opus review of the diff before it is called done**; escalate a tier on ambiguity or two failures, with "default up" **capped at Opus**. Above that sits the Fable `expert` — verdicts and arbitration only, reached by the trigger phrases "use expert" / "deep thinking" / "deep investigate" / naming Fable, or auto (announced, one line) only when an opus agent failed twice / reported low confidence, or two opus verdicts deadlock. Multi-agent fan-out only on explicit trigger phrases ("fan this out", "workflow this", "grind on this", "do this properly").

## Skills

| Skill | Trigger | What it does |
|---|---|---|
| `/review-pr [n]` | "review PR 42" | Sonnet lens agents read the diff, the Opus main loop adjudicates, and inline comments are **staged as a PENDING review on GitHub** (never submitted — only you see them). Handles your existing reviews: a stale pending review is cleared and redone; posted threads are left alone, resolved when no longer relevant, or get a **staged** reply when someone responded. Chat report gives a ready / neutral / seriously-problematic verdict. |
| `/resolve-ticket [ticket-id]` | "fix ENG-123" | Resolves the ticket from Linear or Jira (first unknown prefix: asks once, remembers per repo in `local/resolve-ticket.json`), pulls linked Confluence/Slack/GitHub context, investigates and plans at Opus, **waits for explicit sign-off**, implements on a worktree branch with Haiku/Sonnet executors, Opus-reviews the diff, then pushes and opens a draft PR in the browser. |
| `/watch-review` | `/loop 1m /watch-review` | One polling tick: finds open non-draft PRs in the cwd's repo where you are **directly** requested as reviewer (team requests don't count), runs `/review-pr` on each new one, and records it in `local/review-watcher.json` so a PR is never auto-reviewed twice. Idle ticks run on Haiku and cost almost nothing. |

## Synced vs machine-local

| Synced (this repo) | Machine-local (never committed) |
|---|---|
| `agents/`, `skills/`, `hooks/`, `workflows/` (symlinked); `mcp.list` + `claude/scripts/` (used from the repo path) | `local/` — per-repo skill/agent state JSON |
| Global `CLAUDE.md` (via `@import`) | `~/.claude/CLAUDE.md` content below the `@import` line |
| `settings.json` (populated: repo keys merged into `~/.claude/settings.json`) | Extra keys you add to `~/.claude/settings.json`; `settings.local.json` files |
| | MCP registrations + OAuth state in `~/.claude.json` |
| | Runtime state: `sessions/`, `projects/`, `plans/`, `backups/`, plugin installs |

Secrets and API keys never go in this repo — keep them in environment variables or `settings.local.json` files.

## MCP servers

`install.sh mcp` registers every server in [claude/mcp.list](claude/mcp.list) at user scope (skipping ones already present). Registration is deliberately opt-in — work machines may not want personal servers. Two caveats:

- **OAuth (Linear, Atlassian)**: registration ≠ authentication. Run `/mcp` inside a session once per machine to complete the browser OAuth flow.
- **Slack**: Slack's hosted MCP doesn't support the standard OAuth flow Claude Code uses (no dynamic client registration). One-time setup: create a Slack app for your workspace, complete its OAuth manually to mint a token, and export it as `SLACK_MCP_TOKEN` before starting Claude Code (e.g. in your shell profile). The manifest passes it as a bearer header, unexpanded, so the token itself is never written to disk by this repo.

## Using the shared workflows

`~/.claude/workflows` is symlinked to this repo and picked up globally (verified: the workflows appear as invocable by name in any session). In any project, ask Claude to "run the cost-tiered-fix workflow" with a goal or an explicit task list. A project can also carry its own `.claude/workflows/` for repo-specific scripts.

## Editing flow

Edit files here (directly or through a Claude session — the symlinked content dirs mean edits made via `~/.claude` land in the repo too), commit, push. Other machines pick it up on their next `pull`. Two special cases: `claude/settings.json` is populated, so edit the **repo copy** for portable preferences (then re-run `install.sh`) and `~/.claude/settings.json` directly for machine-only ones; skill state under `local/` is deliberately not synced — there is nothing to commit when mappings or watcher history change.
