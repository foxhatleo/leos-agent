# Setup delta — OpenCode

Host-specific steps layered on the shared `docs/SETUP.md` interview.

1. **Detect** `~/.config/opencode`. Confirm `opencode --version`.
2. **Symlinks** — `bin/leos-python bin/leos-link.py --tool opencode` (plugin, private launcher,
   council.py, skill dir). The plugin destination is `~/.config/opencode/plugins/` (plural).
   Global instructions are NOT a `~/.config/opencode/AGENTS.md` symlink (which would clobber the
   user's own) — they come via the `instructions[]` entry in step 3. Remove a leftover
   `~/.config/opencode/AGENTS.md` clone-symlink from an older install (safe — it's a symlink;
   `leos-doctor` flags it).
3. **Permissions + instructions merge** — `bin/leos-python bin/leos-merge.py --tool opencode` merges
   `opencode-fragment.json` into `~/.config/opencode/opencode.json`: `permission.read` secret denies
   + `permission.bash` allows, **plus** an additive `instructions` entry whose `{{CLONE_ROOT}}`
   token is expanded to the clone path (so `global/AGENTS.md` loads alongside the user's own
   `AGENTS.md`, live on pull). `deny` is enforced even in auto mode; avoid `"ask"` in pure headless
   `serve`. Verify `opencode.json` `instructions` contains the absolute `<clone>/global/AGENTS.md`.
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
   rely on the global `AGENTS.md` council mandate and invoking the skill. `--agent plan` is an
   agent-policy mode, not a blanket OS-level read-only guarantee; smoke-test the installed version.
   `format-on-edit` is not wired on OpenCode (no PostToolUse exit-2 surface); formatting falls to
   the project's own tooling.
