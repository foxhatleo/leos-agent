# Setup delta — OpenCode

Host-specific steps layered on the shared `docs/SETUP.md` interview.

1. **Detect** `~/.config/opencode`. Confirm `opencode --version`.
2. **Symlinks** — `python3 bin/leos-link.py --tool opencode` (plugin, council.py, skill dir, global
   AGENTS.md).
3. **Permissions merge** — `python3 bin/leos-merge.py --tool opencode` merges
   `opencode-fragment.json` (`permission.read` secret denies + `permission.bash` allows) into
   `~/.config/opencode/opencode.json`. `deny` is enforced even in auto mode; avoid `"ask"` in pure
   headless `serve`.
4. **Guard plugin smoke test** (the guard here is a plugin, so verify it actually blocks):
   ```
   opencode run --agent build -m <cheap-model> 'run: rm -rf ~'
   ```
   Expect the plugin to throw / refuse the bash call. Also confirm the plugin loaded (OpenCode logs
   a plugin-load line). If the `tool.execute.before` API shape differs in this OpenCode version,
   adjust `plugin/leos-guard.ts` (it is the single source; edit in the clone).
5. **Council seats** — write `local/seats.opencode.json`: native = `opencode run --agent plan` on the
   host's own model; externals = roster minus the host's model. Resolve slugs at setup.
6. **Caveats:** OpenCode has no documented Stop-event hook, so there is no automatic council nudge —
   rely on the global `AGENTS.md` council mandate and invoking the skill. `format-on-edit` is not
   wired on OpenCode (no PostToolUse exit-2 surface); formatting falls to the project's own tooling.
