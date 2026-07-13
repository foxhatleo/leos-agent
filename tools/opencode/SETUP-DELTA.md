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
5. **Council seats** — install through `bin/leos-seats.py` into the unified `seats[]` array (no
   top-level `native`). OpenCode's own-provider seat is `mode: exec` — `opencode run --agent plan`
   on the host's own model (minTier per the role it fills). The seven target roles, their transport
   preference (best → fallback), and `minTier` presets: **opus** (subagent on Claude only; on
   OpenCode = `mode: exec` via OpenRouter, `opus-4.8` / `claude-opus-4-8`, minTier 1), **gpt**
   (codex → cursor → opencode; on OpenCode = `mode: exec` via OpenRouter, `gpt-5.6-sol` per the
   OpenAI flavor rule — most capable flavor of the newest GPT generation, minTier 2), **grok**
   (cursor → opencode; `grok-4.5`, minTier 3), **glm** (cursor → opencode; `glm-5.2`, minTier 4),
   **gemini** (cursor → opencode; `gemini-3.1-pro`, minTier 4), **mimo** (opencode only — this is
   its only transport; `xiaomi/mimo-v2.5-pro`, minTier 4), **deepseek** (opencode only — this is
   its only transport; `deepseek/deepseek-v4-pro`, minTier 4). On an OpenCode host every seat is
   `mode: exec` (OpenCode has no subagent primitive). OpenCode is the preferred transport for glm,
   gemini, grok when cursor is absent, and the only transport for mimo and deepseek (cursor is
   excluded for deepseek: `reasoning_content` replay bug + 200K cap vs opencode's full 1M).
   **Best effort:** install a seat only if its best available transport is installed AND its driver
   smoke passes; silently drop the rest. For any `codex` argv, NEVER override `CODEX_HOME` (it
   throws away host auth; isolation = `--ephemeral` + scratch cwd + `LEOS_COUNCIL_SEAT`; doctor
   rejects `env.CODEX_HOME` on codex seats). Optional per-seat `envFile`
   (`local/council/env/<seat>.env`, 0600, gitignored) for secrets — inline `env` is non-secret only.
   After each exec seat's smoke passes, validate → write (on OpenCode all seats are `mode: exec`,
   so all need `--confirm-smoke`):
   ```sh
   bin/leos-python bin/leos-seats.py validate --host opencode --input local/seats-candidate.opencode.json
   bin/leos-python bin/leos-seats.py write --host opencode --input local/seats-candidate.opencode.json \
     --confirm-smoke <exec-seat-1> --confirm-smoke <exec-seat-2>
   ```
   **Migration:** an old-shape `seats.opencode.json` (top-level `native`, missing `mode`/`minTier`)
   is rejected by doctor — regenerate via SETUP step 5 + `leos-seats.py write`.
6. **Caveats:** OpenCode has no documented Stop-event hook, so there is no automatic council nudge —
   rely on the global `AGENTS.md` council mandate and invoking the skill. `--agent plan` is an
   agent-policy mode, not a blanket OS-level read-only guarantee; smoke-test the installed version.
   `format-on-edit` is not wired on OpenCode (no PostToolUse exit-2 surface); formatting falls to
   the project's own tooling. The installed CLI exposes no verified no-session-persistence flag;
   disclose that before approving this external transport.
