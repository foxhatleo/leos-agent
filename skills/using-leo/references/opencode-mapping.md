## OpenCode harness mapping

You are running in OpenCode. The roles and tiers above map onto OpenCode's plugin config, not native Claude Code constructs.

### Tiers → models

| Tier | Default model | Override env var |
|---|---|---|
| Opus | `openrouter/z-ai/glm-5.2` | `LEO_MODEL_OPUS` |
| Sonnet | `openrouter/minimax/minimax-m3` | `LEO_MODEL_SONNET` |
| Haiku | `openrouter/deepseek/deepseek-v4-pro` | `LEO_MODEL_HAIKU` |

Whichever value is active for a session (default or override) is what "Opus" / "Sonnet" / "Haiku" mean below — see "Active tier models" at the end of this block for the values resolved for this session.

### No Fable

This harness has no Fable rung. Auto-escalation caps at Opus: on either auto-escalation trigger from the routing table, stop and report to Leo instead of spawning anything further, offering to continue at the Opus tier here or hand off to a harness that has the expert rung (Claude Code).

### Roles → agents

`investigator`, `planner`, `reviewer`, `implementer`, `executor`, and `Explore` are registered as OpenCode agents (from this plugin's `agents/` frontmatter) and are spawned via the task tool as subagents. There is no `expert` agent — it is dropped, per "No Fable" above. There is no per-spawn model override here: each agent always runs its registered model, so `reviewer` always runs its full Opus-tier model — the trivial-diff Sonnet-tier downscale described in the execute-then-review section does not apply on this harness; every diff gets the full review.

### Tools

Available to agents on this harness: bash, read, grep, glob, edit, write, webfetch, todowrite, and the skill tool (`leo:<name>`).

### Worktrees

No EnterWorktree/ExitWorktree tool exists here. Use leo:worktrees' raw-git fallback (plain `git worktree` commands) for isolated branch work.

### State

Machine-local state reads and writes go through `python3 <repo>/scripts/state.py` (`get` / `merge` / `path`), same contract as the policy above.

### Exclusions

No Workflow tool and no `cost-tiered-fix.js` here — a batch of independent tasks is fanned out as manual parallel task-tool subagent spawns instead. MCP servers are not injected through this mapping; they are configured separately in `opencode.json`.

Guard note: the bash deletion tripwire is confirmed for the primary agent. Whether `tool.execute.before` also intercepts subagent bash is unconfirmed (opencode#5894); write-capable agents therefore carry coarse `rm -rf` deny patterns as a stopgap, and OpenCode's own permission layer (`external_directory` asks for out-of-tree writes) remains the outer defense.

using-leo is already loaded in this block — do not re-load it via the skill tool.
