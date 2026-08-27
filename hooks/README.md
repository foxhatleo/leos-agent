# Hooks

Both files here ship empty on purpose. v10 enforces its policy through the
payload and the skills, not by intercepting tool calls — but the wiring is in
place, so adding a hook is an edit rather than a plumbing exercise.

**There are two files because the harnesses disagree on the format.** Claude
Code and Codex use PascalCase event names and no version key; Cursor uses
camelCase names wrapped in `{"version": 1, ...}`. A single file cannot satisfy
both, so each manifest points at its own.

| File | Read by | Wired via |
|---|---|---|
| `hooks.json` | Claude Code, Codex | auto-discovery — **neither manifest may name it** |
| `hooks-cursor.json` | Cursor | `.cursor-plugin/plugin.json`, which overrides Cursor's own auto-discovery |

Both Claude Code and Codex load `hooks/hooks.json` on their own. Declaring it in
the manifest as well is a duplicate: Codex's validator rejects the key outright,
and Claude Code fails the entire plugin at load time with `Duplicate hooks file
detected` — which `claude plugin validate` does **not** catch, so only a real
install reveals it. `scripts/check.py` guards both cases.

Cursor is the exception, and only because its file has a different name: naming
it explicitly overrides Cursor's auto-discovery, which is what keeps Cursor from
trying to read the PascalCase `hooks.json` it cannot parse.

Hermes hooks are different again — they are Python callbacks registered from
`register(ctx)` in `__init__.py` (`pre_tool_call`, `post_tool_call`,
`on_session_start`, and so on), not JSON. OpenCode's are JavaScript hooks
returned from the plugin factory in `index.js`. Pi's are extension event
handlers. All three would be written in code rather than added here.

## Adding one

Claude Code and Codex (`hooks.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/my-check.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Codex exposes the same directory as `${PLUGIN_ROOT}` and accepts
`${CLAUDE_PLUGIN_ROOT}` as an alias, so one command string serves both. A hook
script reads the event JSON on stdin and writes its decision to stdout; exit
code 2 blocks the call, with stderr as the reason.

Cursor (`hooks-cursor.json`) uses the same idea with its own names:

```json
{
  "version": 1,
  "hooks": {
    "preToolUse": [
      { "command": "./hooks/my-check.py", "timeout": 10 }
    ]
  }
}
```

After editing either file, re-run `python3 scripts/check.py` — it validates
that both still parse and that Cursor's keeps `version: 1`. Codex additionally
hash-pins hooks for trust, so a changed hook must be re-approved through
`/hooks` there.
