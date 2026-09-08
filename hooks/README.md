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
Codex's documented rewrite format requires an allow decision; this cost guard
uses rejection with a precise retry instead of granting permission. Native
profile precedence must also be respected. These are cost guardrails, not a
sandbox or proof that every specialized tool path is intercepted.

Cursor checks the resolved subagent_model at subagentStart and returns a native
deny only when needed. Missing prices are allowed with a log diagnostic. It
never invents a model argument for Task. SubagentStop records lifecycle
completion separately from actual model observation.

Claude/Codex SubagentStop reads only a bounded tail of the child's transcript
to observe its latest response model. Transcript formats can change; missing
observations remain unknown. The observer emits empty JSON and never asks a
child to continue. Claude PostModelSwitch refreshes its parent-model cache.
Codex supplies its active model directly in tool-hook events.

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
