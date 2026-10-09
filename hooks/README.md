# Native hook adapters

| File | Harness | Loading |
|---|---|---|
| hooks.json | Claude Code | Auto-discovered; do not also declare the default file in its manifest. |
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
call id where the harness has one. `scripts/outcome.py` reduces that text to
`Result:`/`Verified:` tokens and the text is discarded; `usage` is taken from the
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
Claude Code gives all SessionEnd hooks one 1.5 s budget, and a plugin hook's
`timeout` cannot raise it (only `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` or a
user-settings hook can), so the SessionEnd hook only starts a detached,
time-bounded reconciliation process and returns. SubagentStop may not fire for
background Agent-tool subagents on some builds or in the VS Code extension;
the report counts those dispatches as having no completion signal.
