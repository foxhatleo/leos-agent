## Codex CLI harness mapping

You are running in Codex CLI. The roles and tiers above are native here, with one gap: this harness has no Fable rung.

### Tiers → models

| Tier | Model |
|---|---|
| Opus | `gpt-5.6-sol` (effort high) |
| Sonnet | `gpt-5.6-terra` (effort medium) |
| Haiku | `gpt-5.6-luna` (effort low) |
| Fable | not available on this harness — no Fable |

There is no per-spawn 1M-context alias to reach for here; the model strings above are used as-is wherever a tier is pinned — agent frontmatter, skill frontmatter, or a `spawn_agent` call.

Because there is no Fable rung, degraded escalation caps at Opus: when the policy above calls for the `expert` role, stop and report the deadlock or low-confidence verdict to Leo instead of auto-escalating. Offer two options — continue arbitration at Sol (the Opus-tier model here), or hand the question off to a Claude Code session where the expert rung exists.

### Roles → subagents

Each role in the routing table is a packaged subagent, dispatched with `spawn_agent` / `wait_agent` using natural language (no per-spawn model-override syntax — the agent's own configuration carries its tier). The six subagents available here: `leo-explore`, `leo-investigator`, `leo-planner`, `leo-implementer`, `leo-executor`, `leo-reviewer`. There is no `leo-expert` — see the Fable gap above.

Planning at the Opus tier uses the main loop directly when the session itself runs at the Sol tier; otherwise dispatch `leo-planner`.

### Orchestration

There is no built-in batch-orchestration script on this harness. A fan-out is manual: dispatch multiple `spawn_agent` calls in parallel and collect results with `wait_agent`, pinning `model` and `effort` per call the same way the Claude-side batch script would. Track progress the same way — through the shared ledger at `python3 "$PLUGIN_ROOT/scripts/state.py"` — since there is no other durable record of a long fan-out here. Fan-outs never auto-use Sol as a Fable substitute; the auto-escalation rules above only ever reach Opus, never further.

`/loop` is not available on this harness. To drive `leo:watch-review` (or any other recurring check) on an interval, wrap it in a shell loop of `codex exec` calls at the desired cadence instead.

### Tools

- Review: `codex review` (with `--base` or `--uncommitted`) is the native review alternative here — reach for it directly, or dispatch `leo-reviewer` for the packaged version of the same judgment. Either way the Opus-tier default from the review-gate rule still applies (Sol effort high), with the same narrow downscale-to-Sonnet-tier exception for a clearly-trivial diff.
- State: `python3 "$PLUGIN_ROOT/scripts/state.py"` (`get` / `merge` / `path`) — same contract as elsewhere, `$PLUGIN_ROOT` is this harness's plugin-root variable.
- Worktrees: sandboxed worktrees on this harness may run with a detached `HEAD`. Treat branch-creation and push steps as out of scope for a sandboxed run — do them in a trusted, non-sandboxed workspace instead.
- Guard: `bash-guard` still runs as a `PreToolUse` deny hook when hooks are enabled here, but it is the secondary line of defense — Codex's own `sandbox_mode` setting is the primary guard against unwanted command execution.
