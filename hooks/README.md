# Native hook adapters

| File | Harness | Loading |
|---|---|---|
| hooks.json | Claude Code | Auto-discovered; do not also declare the default file in its manifest. |
| claude-spawn.js | Claude Code | Hooks module (mod) that hooks.json names under `modules`. |
| hooks-codex.json | Codex | Explicit manifest override replaces default discovery. |
| hooks-cursor.json | Cursor | Explicit manifest override with Cursor event names. |

Claude/Codex commands set LEOS_AGENT_HARNESS explicitly. Their SessionStart
hooks emit the compact deterministic policy; Claude also covers fork starts.
Cursor loads its native rule directly, so its lifecycle hook emits no policy.

Claude PreToolUse can supply updatedInput without granting tool permission.
The 2026-09-08 local smoke tests verified this exact output shape by observing
actual Haiku/Sonnet child models, including a Sonnet-to-Haiku correction; see
README.md for the scope and cold-session limitation.
Codex's documented rewrite format requires an allow decision; this cost guard
uses rejection with a precise retry instead of granting permission. Native
profile precedence must also be respected. These are cost guardrails, not a
sandbox or proof that every specialized tool path is intercepted.

Cursor checks the resolved subagent_model at subagentStart and returns a native
deny only when needed. Missing prices are allowed with a log diagnostic. It
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
observations remain unknown. The observer emits empty JSON and never asks a
child to continue. Claude PostModelSwitch refreshes its parent-model cache.
Codex supplies its active model directly in tool-hook events.

Completion signals share one contract. Every adapter sends the observer a
SubagentStop-shaped event carrying at most the last 4 KiB of the child's final
text under the harness's own key (`last_assistant_message` on Claude and Codex,
`child_summary` on Hermes, `result_text` from the JavaScript adapters) plus a
call id where the harness has one. `scripts/outcome.py` reduces that text to
`Result:`/`Verified:` tokens and the text is discarded; `usage` is taken from the
event or, on Claude and Codex, summed from the child transcript. Cursor's
subagentStop has a status token and no text, so its rows say status-only.
OpenCode uses `tool.execute.after` on `task`; Pi uses `tool_result` on
`subagent`; Hermes registers `subagent_stop` and `post_tool_call` and tolerates a
build that refuses either. The guard records the brief's `Escalation from
<tier>:` header as a tier token; on Codex the brief is encrypted and the field
reads `unobservable`.

Scripts live in scripts/ and are included in the npm package. Hook input is
bounded; errors fail open with local diagnostics. No ordinary dispatch makes a
network or model call. Price refresh is a separate bounded background process.
Codex hook changes require the user's native /hooks trust review; installation
does not silently approve them.

Hermes uses Python callbacks in __init__.py. OpenCode and Pi use their native
JavaScript plugin/extension APIs. All decisions share routing_engine.py;
adapters only supply observations and translate supported actions.

References:

- [Claude hooks](https://code.claude.com/docs/en/hooks)
- [Codex hooks and plugin overrides](https://learn.chatgpt.com/docs/hooks)
- [Cursor hooks](https://cursor.com/docs/hooks)
- [Cursor plugin format](https://cursor.com/docs/reference/plugins)

Claude live verification established that Agent accepts the aliases haiku,
sonnet, opus, and fable, rather than full transcript model IDs. The guard
translates an observed parent to an alias only after checking its price.
Fresh-session PreToolUse may run before the first assistant response is
written: the parent is then unavailable and the approved unknown-price policy
allows dispatch with a diagnostic. SubagentStop can likewise precede the child
transcript flush; SessionEnd reconciles those observations without model calls.

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
