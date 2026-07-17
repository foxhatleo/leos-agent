## Claude Code harness mapping

You are running in Claude Code. The roles and tiers above are native here.

### Tiers → models

| Tier | Model |
|---|---|
| Opus | Claude Opus (`opus[1m]`) |
| Sonnet | Claude Sonnet (`sonnet[1m]`) |
| Haiku | Claude Haiku (`haiku`) |
| Fable | Claude Fable (`fable`) |

**Concrete model strings use the 1M-context aliases.** Wherever a model is pinned — agent frontmatter, skill frontmatter, or a workflow `agent()` call — Opus means `opus[1m]` and Sonnet means `sonnet[1m]` (Haiku and Fable are unchanged; tier names in the routing table stay plain words). The one exception: a per-spawn Agent-tool `model` override is a strict `sonnet|opus|haiku|fable` enum with no `[1m]` variants — overrides pass the plain alias, and a subagent gets 1M context by inheriting its frontmatter default (spawn with no override), never through an override.

### Roles → subagents

Each role in the routing table is a packaged subagent, spawned via the Agent tool: `investigator`, `planner`, `implementer`, `executor`, `reviewer`, `expert`, and `Explore`. Spawn with **no model override** so each inherits its frontmatter default (that is how a subagent gets its `[1m]` context). The Sonnet-tier review downscale is a per-spawn `model: sonnet` override on `reviewer`. Planning at the Opus tier uses plan mode in the main loop when the session itself is Opus; otherwise spawn `planner`.

### Orchestration

Multi-agent orchestration uses the Workflow tool. The reusable batch workflow lives at `${CLAUDE_PLUGIN_ROOT}/workflows/cost-tiered-fix.js` (invoke via `scriptPath`). In workflow scripts set `model` and `effort` per `agent()` call; workflow scripts never auto-use `fable`.

### Tools

Native here: Skill tool (`leo:<name>`), EnterWorktree/ExitWorktree (preferred by leo:worktrees), plan mode, MCP servers from the plugin's `.mcp.json`.
