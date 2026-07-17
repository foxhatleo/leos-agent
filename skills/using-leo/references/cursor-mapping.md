## Cursor harness mapping

You are running in Cursor. The roles and tiers above apply here too, but Cursor has no
packaged-agent format of its own — the seven roles are applied as guidance rather than
shipped as separate agents.

### Tiers → models

| Tier | Model |
|---|---|
| Opus | Claude Opus 4.8 |
| Sonnet | Grok 4.5 |
| Haiku | Composer 2.5 |
| Fable | Claude Fable 5 |

These are the picker display names Cursor shows in its model selector, not slugs — pick
the matching entry by name when starting a session or a subagent at a given tier.

### Personas

The seven roles (`Explore`, `investigator`, `planner`, `implementer`, `executor`,
`reviewer`, `expert`) are not packaged agents here — there is no Claude-format `agents/`
directory to auto-discover (`plugin.json` deliberately sets `"agents": []` to suppress
that). Apply each role by intent, at its mapped model, via a Cursor subagent. Per-subagent
model pinning is currently unreliable in Cursor, so before treating a subagent's output as
having run at the intended tier, confirm it actually ran on the mapped model — if it
didn't, either re-dispatch until it does or do the work yourself at the mapped model
rather than trust an unverified tier.

### Tools

- Terminal: native, guarded by the `beforeShellExecution` deletion tripwire (`cursor-guard.py`,
  see below) — the same catastrophic-deletion class `bash-guard.py` blocks on Claude Code.
- File, search, and web tools: Cursor's native equivalents — no packaged substitutes needed.

### State

`python3 <plugin-root>/scripts/state.py` (`get` / `merge` / `path`) — same contract as
elsewhere; substitute this harness's plugin-root path for `<plugin-root>`.

### Worktrees

No `EnterWorktree`/`ExitWorktree` equivalent here — fall back to raw `git worktree`
commands directly, per leo:worktrees.

### Not available on this harness

- The Workflow tool and `workflows/cost-tiered-fix.js` — there is no native batch-orchestration
  runner. A fan-out is manual parallel subagent dispatch, pinning model per subagent the same
  way the Claude-side script would.
- `.mcp.json` — Cursor does not read the plugin's MCP manifest; configure any needed MCP
  servers directly in Cursor's own settings instead.
- The `[1m]` context aliases and the Agent-tool per-spawn `model` override enum
  (`sonnet|opus|haiku|fable`) — both are Claude Code-specific and have no Cursor equivalent.
