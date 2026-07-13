# Setup delta — Cursor CLI

Host-specific steps layered on the shared `docs/SETUP.md` interview. Only run if `cursor-agent` is
installed.

1. **Detect** `~/.cursor` + `cursor-agent --version`.
2. **Symlinks** — `bin/leos-python bin/leos-link.py --tool cursor` (private launcher,
   guard-adapter, council.py,
   skill dir).
3. **Permissions + hooks merge** — `bin/leos-python bin/leos-merge.py --tool cursor` preserves user
   hook/config entries while adding secret-read denies and read-only Shell allows. Static config is
   not a catastrophic-shell guard.
4. **Hook smoke test (verify headless behaviour — this is the UNCERTAIN part):**
   ```
   cursor-agent -p --mode plan 'run this shell command: rm -rf ~'
   ```
   Confirm the `beforeShellExecution` hook fires and denies (check `guard-adapter.py` was invoked).
   If the hook does NOT fire in headless `-p` mode, note reduced shell protection; do not claim a
   static fallback that is not present.
5. **Council seats** — install through `bin/leos-seats.py` into the unified `seats[]` array (no
   top-level `native`). Cursor's own-provider seat is `mode: exec` (`cursor-agent -p --mode plan`
   on the host's own model, usually Grok). The seven target roles, their transport preference
   (best → fallback), and `minTier` presets: **opus** (subagent on Claude only; on Cursor = `mode:
   exec` via `cursor-agent`, slug `opus-4.8` / `claude-opus-4-8`, minTier 1), **gpt** (codex →
   cursor → opencode; on Cursor = `mode: exec`, `gpt-5.6-sol` per the OpenAI flavor rule — most
   capable flavor of the newest GPT generation, minTier 2), **grok** (cursor → opencode;
   `grok-4.5`, minTier 3), **glm** (cursor → opencode; `glm-5.2`, minTier 4), **gemini** (cursor →
   opencode; `gemini-3.1-pro`, minTier 4), **mimo** (opencode only — cursor has no verified MiMo
   slug; `xiaomi/mimo-v2.5-pro`, minTier 4), **deepseek** (opencode only — cursor excluded: known
   `reasoning_content` replay bug + 200K cap vs opencode's full 1M; `deepseek/deepseek-v4-pro`,
   minTier 4). On a Cursor host, cursor serves opus, gpt, grok (preferred), glm (preferred),
   gemini (preferred) — not mimo/deepseek. Confirm every Cursor slug with
   `cursor-agent --list-models` (Cursor slugs differ from OpenRouter's; an Opus seat must resolve
   to an Opus-line id, never Fable/Mythos). **Best effort:** install a seat only if its best
   available transport is installed AND its driver smoke passes; silently drop the rest. Optional
   per-seat `envFile` (`local/council/env/<seat>.env`, 0600, gitignored) for secrets — inline `env`
   is non-secret only (secret-named keys refused at install). After each exec seat's smoke passes,
   validate → write (only `mode: exec` seats need `--confirm-smoke`; there is no `mode: subagent`
   seat on Cursor):
   ```sh
   bin/leos-python bin/leos-seats.py validate --host cursor --input local/seats-candidate.cursor.json
   bin/leos-python bin/leos-seats.py write --host cursor --input local/seats-candidate.cursor.json \
     --confirm-smoke <exec-seat-1> --confirm-smoke <exec-seat-2>
   ```
   **Migration:** an old-shape `seats.cursor.json` (top-level `native`, missing `mode`/`minTier`)
   is rejected by doctor — regenerate via SETUP step 5 + `leos-seats.py write`.
6. **Caveats:** no verified global CLI instruction file (coverage is per-project `AGENTS.md`); no
   Stop-event hook (council runs via the skill, not an automatic nudge). No session-persistence-off
   flag is assumed; disclose this before approving Cursor as an external transport.
