# leo

A portable personal Claude Code environment, packaged as a Claude Code plugin: cost-tiered model routing, 7 subagents, 13 skills, a bash-guard `PreToolUse` hook, MCP servers, and machine-local state — one clone, works on any machine.

The routing policy isn't a doc you have to remember to read — a `SessionStart` hook injects it as context at the start of every session, and again after `/clear` and after compaction, so it survives the moments that normally wipe it.

## Install

```sh
git clone git@github.com:foxhatleo/leos-agent.git ~/.leos-agent
~/.leos-agent/install.sh migrate    # v2 cleanup: drops old symlinks/imports; harmless on a fresh machine
~/.leos-agent/install.sh settings   # merges portable settings.json keys into ~/.claude/settings.json
claude plugin marketplace add ~/.leos-agent
claude plugin install leo@leos-agent
~/.leos-agent/install.sh check      # confirms wiring, exits 1 on drift
```

## Update

```sh
git -C ~/.leos-agent pull
~/.leos-agent/install.sh update
```

A marketplace install is a cached copy, not a symlink — `install.sh update` re-syncs what a plugin install can't pick up on its own (settings keys, machine-local wiring), and a version bump in `.claude-plugin/plugin.json` is what actually ships agent/skill/hook changes through `claude plugin`.

### Dev loop

`claude --plugin-dir ~/.leos-agent` loads the clone in place — edits are live immediately, no reinstall, no version bump.

## Layout

```
.claude-plugin/
  plugin.json                   name "leo", version, metadata
  marketplace.json               self-listed marketplace "leos-agent" (source: ./)
.mcp.json                       MCP server manifest, auto-registered when the plugin is enabled
install.sh                      migrate / settings / check / update
local/                          machine-local state (gitignored, per-repo/project JSON)
scripts/
  state.py                      shared get/merge/path helper for local/ state files
agents/
  explore.md                    haiku, read-only — code location and structure mapping
  executor.md                   haiku — mechanical, well-specified grunt work
  implementer.md                sonnet[1m] — executes an approved plan
  planner.md                    opus[1m], read-only — turns a goal into a concrete plan
  investigator.md               opus[1m], read-only — root-cause diagnosis
  reviewer.md                   opus[1m], read-only — diff verdicts, confidence-gated findings
  expert.md                     fable, read-only — ceiling tier: hardest verdicts and
                                arbitration only, manual triggers or rare auto-escalation
skills/
  using-leo/                    the routing policy itself — injected by the SessionStart hook,
                                not model-invoked
  review-pr/                    /leo:review-pr — stage a pending GitHub PR review + verdict
  resolve-ticket/                /leo:resolve-ticket — ticket to draft PR with a sign-off gate
  watch-review/                 /leo:watch-review — poll for direct review requests (via /loop)
  debugging/                    root-cause-before-fix loop for bugs and failing tests
  verification/                 fresh-evidence gate before claiming done/fixed/passing
  test-first/                   failing-test-first default for runtime-behavior changes
  writing-plans/                quality bar for plans an implementer can execute unaided
  executing-plans/               checkpoint discipline for carrying out a written plan
  brainstorming/                 design gate before non-trivial code, scaled to blast radius
  worktrees/                     worktree lifecycle mechanics for isolated branch work
  finishing-a-branch/            end-of-branch state machine: merge / PR / keep / discard
  delegation/                     dispatch mechanics for subagents, single-spawn or fan-out
hooks/
  hooks.json                    registers the two hooks below
  session-start.py              SessionStart: injects skills/using-leo/SKILL.md as context
  bash-guard.py                 PreToolUse(Bash): blocks catastrophic deletions only
workflows/
  cost-tiered-fix.js            batch-fix workflow: Opus plans/verifies, Haiku/Sonnet execute
tests/                          pytest suite: guard, state, PR-review helper, config consistency
```

## Agents

| Agent | Tier | What it's for |
|---|---|---|
| `Explore` | haiku, read-only | Fast codebase scouting — locate files, definitions, usages. Returns file:line, never a verdict. |
| `executor` | haiku | Mechanical, well-specified work — renames, boilerplate, a known pattern applied across files. Fan out in parallel. |
| `implementer` | sonnet[1m] | Executes an approved plan or well-scoped spec needing local judgment but no design decisions. |
| `planner` | opus[1m], read-only | Turns a goal (or an investigator's findings) into a concrete, step-by-step implementation plan. Never edits. |
| `investigator` | opus[1m], read-only | Root-cause diagnosis — evidence and a verdict, never a fix. Spawn one per question. |
| `reviewer` | opus[1m], read-only | Judges a diff — confidence-scored findings, approved / needs-changes. Never fixes what it finds. |
| `expert` | fable, read-only | Ceiling tier for the hardest verdicts and arbitration. Manual trigger phrases, or rare announced auto-escalation — never a default. |

## Skills

**Policy (injected, not model-invoked)**

| Skill | What it is |
|---|---|
| `using-leo` | The routing policy: model tiers, execute-then-review, delegation rules, orchestration triggers, machine-local state, skill index. Injected by the SessionStart hook every session, `/clear`, and compaction. |

**Operational (slash-invoked)**

| Skill | Trigger | What it does |
|---|---|---|
| `/leo:review-pr [n]` | "review PR 42" | Reads the diff, stages inline comments as a PENDING GitHub review (never submitted), reports a ready/neutral/problematic verdict. Handles existing reviews: stale pending ones are redone, posted threads left or replied to as staged. |
| `/leo:resolve-ticket [id]` | "fix ENG-123" | Ticket to draft PR: pulls linked Confluence/Slack/GitHub context, investigates and plans at Opus, waits for explicit sign-off, implements on a worktree branch with Sonnet/Haiku, Opus-reviews, pushes and opens a draft PR. |
| `/leo:watch-review` | `/loop 1m /leo:watch-review` | One polling tick: finds PRs where you're directly requested as reviewer, runs `/leo:review-pr` on each new one, records it in machine-local state so nothing is reviewed twice. Idle ticks run cheap on Haiku. |

**Process (skill-index entries, reached at the matching decision point)**

| Skill | When |
|---|---|
| `debugging` | A bug or failing test, before any fix — Reproduce, Localize, Hypothesize, Prove, Fix. |
| `verification` | Before claiming anything done, fixed, or passing — needs a proving command run this turn. |
| `test-first` | Adding or changing runtime behavior — write the failing test before the fix. |
| `writing-plans` | Turning a chosen approach into a plan a Sonnet implementer can execute unaided. |
| `executing-plans` | Carrying out a written plan — batch execution, a check at every boundary. |
| `brainstorming` | An approach not yet settled, before non-trivial code — gate scales with blast radius. |
| `worktrees` | Isolating branch work in a git worktree. |
| `finishing-a-branch` | A branch's implementation is done — merge / PR / keep / discard. |
| `delegation` | Dispatching subagents — brief construction, model/effort pinning, fan-out mechanics. |

## Model routing (summary)

Canonical copy lives in [skills/using-leo/SKILL.md](skills/using-leo/SKILL.md) — that's the file the SessionStart hook injects, so it's always what a session is actually running on. Unchanged from prior versions: Opus for investigation/planning/review, Sonnet as default executor, Haiku for mechanical work; every execute request ends with an Opus review of the diff before it's called done; escalate a tier on ambiguity or two failures, "default up" capped at Opus. Above that sits the Fable `expert` — verdicts and arbitration only, reached by trigger phrases ("use expert" / "deep thinking" / "deep investigate" / naming Fable) or rare announced auto-escalation. Multi-agent fan-out only on explicit trigger phrases ("fan this out", "workflow this", "grind on this", "do this properly").

## MCP servers

Server definitions ship in [.mcp.json](.mcp.json) (Linear, Atlassian, Slack) and auto-register the moment the `leo` plugin is enabled — no separate registration step.

- **OAuth (Linear, Atlassian)**: enabling the plugin wires the server, not the login. Run `/mcp` inside a session once per machine to complete the browser OAuth flow.
- **Slack**: Slack's hosted MCP doesn't support Claude Code's standard OAuth flow. One-time setup: create a Slack app for your workspace, complete its OAuth manually to mint a token, export it as `SLACK_MCP_TOKEN` before starting Claude Code. `.mcp.json` passes it as a bearer header, unexpanded — the token is never written to disk by this repo.
- **Opt-out**: don't want personal MCP servers on a work machine — install the plugin without adding `SLACK_MCP_TOKEN` and skip `/mcp` for the servers you don't want authenticated; or disable the plugin's MCP config from `claude plugin`.

## Machine-local state

Anything a skill or agent persists is JSON under `${LEOS_AGENT_PATH:-~/.leos-agent}/local/<name>.json`, written through the plugin's own `scripts/state.py` (`get` / `merge` / `path`) rather than hand-rolled read-modify-write. Top-level keys are `owner/repo` (or the absolute project path with no GitHub repo) — data always stays separate per repo/project. This directory is gitignored, never synced, and survives plugin updates by design — a version bump in `plugin.json` ships new agents/skills/hooks but never touches `local/`. Examples: `review-watcher.json` (PRs already auto-reviewed), `resolve-ticket.json` (ticket-prefix to tracker mappings).

## Synced vs machine-local

| Synced (this repo, ships via the plugin) | Machine-local (never committed) |
|---|---|
| `agents/`, `skills/`, `hooks/`, `workflows/`, `scripts/`, `.mcp.json` | `local/` — per-repo/project skill and agent state JSON |
| `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` | MCP OAuth tokens, `SLACK_MCP_TOKEN` |
| Portable keys in `settings.json` (merged via `install.sh settings`) | Everything else in `~/.claude/settings.json`; `settings.local.json` files |
| | Runtime state: `sessions/`, `projects/`, `plans/`, `backups/`, plugin install cache |

Secrets and API keys never go in this repo — keep them in environment variables or `settings.local.json` files.

## v2 → v3

v2 was a dotfiles-style repo: `install.sh` symlinked `agents/`, `skills/`, `hooks/`, `workflows/` item-by-item into `~/.claude/`, and `CLAUDE.md` picked up the policy via `@import`. v3 packages the same content as a Claude Code plugin (`leo`, self-listed in marketplace `leos-agent`) installed through `claude plugin install`; the policy is no longer an import line in `CLAUDE.md` but a skill (`skills/using-leo/SKILL.md`) injected by a `SessionStart` hook, so it also survives `/clear` and compaction, which `@import` never did. `install.sh migrate` tears down the old symlinks and `@import` line — run it once per machine; it's a no-op on a fresh install. After migrating, updates flow through `claude plugin` and a `plugin.json` version bump instead of a live-editable symlink farm.
