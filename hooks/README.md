# Native hook adapters

| File | Harness | Loading |
|---|---|---|
| hooks.json | Claude Code | Auto-discovered; do not also declare the default file in its manifest. |
| claude-spawn.js | Claude Code | Hooks module (mod) that hooks.json names under `modules`. |
| hooks-codex.json | Codex | Explicit manifest override replaces default discovery. |
| hooks-cursor.json | Cursor | Explicit manifest override with Cursor event names. |

Claude/Codex commands set LEOS_AGENT_HARNESS explicitly. Their SessionStart
hooks emit the compact deterministic policy; both also cover fork starts.
Cursor loads its native rule directly, so its lifecycle hook emits no policy.

Claude PreToolUse can supply updatedInput without granting tool permission.
The 2026-09-08 local smoke tests verified this exact output shape by observing
actual Haiku/Sonnet child models, including a Sonnet-to-Haiku correction; see
README.md for the scope and cold-session limitation.
Codex's documented rewrite format requires an allow decision; this cost guard
uses rejection with a precise retry instead of granting permission. Native
profile precedence must also be respected. These are cost guardrails, not a
sandbox or proof that every specialized tool path is intercepted. Codex hook
input names a multi-agent v2 spawn `collaborationspawn_agent` (namespace and
tool joined with no separator); the matcher and guard accept it alongside
`spawn_agent`. A custom `features.multi_agent_v2.tool_namespace` is not matched.

Cursor checks the resolved subagent_model at subagentStart and always answers:
`allow`, or `deny` with an `agent_message` the model reads. Cursor blocks a
permission hook on invalid JSON or an invalid response, so failures also
answer `allow`. Missing prices are allowed with a log diagnostic. It
never invents a model argument for Task. SubagentStop records lifecycle
completion separately from actual model observation.

The installed Cursor agents carry the no-delegation rule as worker instructions
only. Cursor publishes no per-agent tool restriction, and subagentStart reports
the child's subagent_type and a parent conversation id but not the parent's
agent type, so a hook cannot establish that a dispatch came from a worker rather
than the top-level session. This is weaker than Claude's disallowedTools or
OpenCode's per-agent task: false, and is reported rather than presented as
parity. Any future enforcement must preserve leo-reviewer's delegation to
bounded lenses, which review-pr depends on.

Claude/Codex SubagentStop reads only a bounded tail of the child's transcript
to observe its latest response model. Transcript formats can change; missing
observations remain unknown. The observer injects nothing into the parent. Its
one non-empty reply goes to a leo-* child whose final message lacks the
`Result:`/`Verified:` lines: on a first stop (`stop_hook_active` false) it asks
once for the report restated with them, as `additionalContext` on Claude and
`decision: block` on Codex, and records the child at its next stop. A Claude
child that reported through its hand-back tool is never asked. Guard modes
`warn` and `off` disable the prompt. Claude PostModelSwitch refreshes its
parent-model cache. Codex supplies its active model directly in tool-hook events.

Completion signals share one contract. Every adapter sends the observer a
SubagentStop-shaped event carrying at most the last 4 KiB of the child's final
text under the harness's own key (`last_assistant_message` on Claude and Codex,
`child_summary` on Hermes, `result_text` from the JavaScript adapters) plus a
call id where the harness has one. Claude's SubagentStop has no call id, so a
Claude PostToolUse hook on `Agent|Task` runs `scripts/link_agent.py`: it writes
one row tying the call's `tool_use_id` to `tool_response.agentId`, for a
foreground result and for a background launch, and reads nothing else. The
report joins Claude completions through that row and guesses the nearest
preceding dispatch only when no link exists. `scripts/outcome.py` reduces the
child's text to `Result:`/`Verified:` tokens and the text is discarded; `usage` is taken from the
event or, on Claude and Codex, summed from the child transcript by the usage
scan's rules, with a turn count. A Claude child that reports through its
hand-back tool is read from that report. Cursor's subagentStop carries the
child's `summary`, call id and child conversation id; without a summary the
row says status-only. Background Cursor subagents may send no subagentStop or
null fields. OpenCode uses `tool.execute.after` on `task`, taking the agent from
the executed arguments and the child model from task metadata; a background
task fires it at launch and is not recorded. Pi uses `tool_result` on
`subagent`. Hermes records each child from `subagent_stop`, keyed by its child
session; `post_tool_call` sees a background handle or the same children, so it
records only on a build that never fires `subagent_stop`. The guard records the
brief's `Escalation from <tier>:` header as a tier token; on Codex the brief is
encrypted and the field reads `unobservable`.

Scripts live in scripts/ and are included in the npm package. Hook input is
bounded; errors fail open with local diagnostics. An invalid routing.json keeps
its valid harness sections, uses defaults for the rest, and is recorded as
`routing-config-invalid` in the dispatch and emit logs. No ordinary dispatch makes a
network or model call. Price refresh is a separate bounded background process.
Codex hook changes require the user's native /hooks trust review; installation
does not silently approve them.

Hermes uses Python callbacks in __init__.py. OpenCode and Pi use their native
JavaScript plugin/extension APIs. All decisions share routing_engine.py;
adapters only supply observations and translate supported actions.

Hermes blocks a tool when a `pre_tool_call` callback raises or outlives
`plugins.hook_callback_timeout`, so the callback catches every error and bounds
the guard at 10 s. Hermes spawns whenever `delegate_task`'s action, trimmed and
lowercased, is empty or `spawn`; the guard reads it the same way.

OpenCode executes the `args` object it passes to `tool.execute.before`, so a
correction edits that object in place, and one that cannot be applied blocks the
task. Slash-command subtasks pass through the same hook, but the hook input
omits the command's model, which the task runs on when its agent pins none; the
guard then prices it as the parent. A block there ends the command with an
error instead of a task result.

Pi rows carry the session id. Pi's subagent tools take `{agent, task}` or
`{task}` and never the child's model, so Pi dispatches are logged without a
price check.

References:

- [Claude hooks](https://code.claude.com/docs/en/hooks)
- [Codex hooks and plugin overrides](https://learn.chatgpt.com/docs/hooks)
- [Cursor hooks](https://cursor.com/docs/hooks)
- [Cursor plugin format](https://cursor.com/docs/reference/plugins)

Claude live verification established that Agent accepts the aliases haiku,
sonnet, opus, and fable, rather than full transcript model IDs. The guard caps
a child with the parent's family alias, which Claude runs on the parent's exact
model; when the parent's ID names no family, an inheriting agent gets no
`model` and so runs on the parent. It fills a missing model only for built-in
agents that would inherit (general-purpose, claude, Explore, Plan) and for the
cheap, standard, and premium leo tiers; leo-lens, whose definition inherits,
gets the standard tier when the reviewer names no model. leo-parent and forks
run on the parent; other plugins' agents keep their own model.
Fresh-session PreToolUse may run before the first assistant response is
written: the parent is then unavailable, nothing is filled in except a leo
tier's configured model, and dispatch is allowed with a diagnostic.
SubagentStop can likewise precede the child transcript flush; SessionEnd
reconciles those observations without model calls.
Claude Code gives all SessionEnd hooks one 1.5 s budget, and a plugin hook's
`timeout` cannot raise it (only `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` or a
user-settings hook can), so the SessionEnd hook only starts a detached,
time-bounded reconciliation process and returns. SubagentStop may not fire for
background Agent-tool subagents on some builds or in the VS Code extension;
the report counts those dispatches as having no completion signal.

## Claude agent.spawn mod

Claude Code raises `agent.spawn` after PreToolUse, just before a subagent or
teammate starts, with the resolved agent type, the requested model and the
parent's effective model, so no transcript lookup is needed for the ceiling.
`claude-spawn.js` hands that to `dispatch_guard.py --json`, the same decision
and the same single log row as the command guard, then sets the child's model
or refuses the spawn with the guard's text. It grants no permission and fails
open: if the guard cannot run, the spawn proceeds and the debug log says why.

One side decides each dispatch. On Claude Code 2.1.289 and later, where
`agent.spawn` also covers teammates, the module's `session.start` sets
`LEOS_AGENT_CLAUDE_SPAWN_MOD` to the plugin root in the Claude Code process.
Claude Code starts this plugin's command hooks with `CLAUDE_PLUGIN_ROOT` set to
the same root; when the two agree, the PreToolUse command guard passes Claude
dispatches through without correcting or logging them. Processes the Bash tool
starts inherit the variable but not `CLAUDE_PLUGIN_ROOT`, so tests and manual
runs inside a session still decide. On earlier builds the module clears the
variable, and wherever mods do not load (`disableAllHooks`,
`allowManagedModsOnly`, `--safe-mode`) nothing sets it, so the command guard
decides. Forks, workflow agents and other plugins' own `$.agent.spawn` calls are
left alone. Claude Code 2.1.250 and older reject this hooks file outright, so
none of its hooks load there.

`claude plugin validate .` lists what the module hooks and calls;
`claude plugin test .` runs `tests/claude/` against Claude Code's own engine.
