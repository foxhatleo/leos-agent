# Native hook adapters

| File | Harness | Loading |
|---|---|---|
| hooks.json | Claude Code | Auto-discovered; do not also declare the default file in its manifest. |
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
cheap, standard, and premium leo tiers. leo-parent and forks run on the
parent; other plugins' agents keep their own model.
Fresh-session PreToolUse may run before the first assistant response is
written: the parent is then unavailable, nothing is filled in except a leo
tier's configured model, and dispatch is allowed with a diagnostic.
SubagentStop can likewise precede the child transcript flush; SessionEnd
reconciles those observations without model calls.
