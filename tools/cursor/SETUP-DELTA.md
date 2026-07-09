# Setup delta — Cursor CLI

Host-specific steps layered on the shared `docs/SETUP.md` interview. Only run if `cursor-agent` is
installed.

1. **Detect** `~/.cursor` + `cursor-agent --version`.
2. **Symlinks** — `python3 bin/leos-link.py --tool cursor` (guard-adapter, hooks.json, council.py,
   skill dir).
3. **Permissions merge** — `python3 bin/leos-merge.py --tool cursor` merges
   `permissions-fragment.json` (Read denies + Shell allows) into `~/.cursor/cli-config.json`. This
   static deny list is the **reliable** guard surface; the hook is defence-in-depth.
4. **Hook smoke test (verify headless behaviour — this is the UNCERTAIN part):**
   ```
   cursor-agent -p --mode plan 'run this shell command: rm -rf ~'
   ```
   Confirm the `beforeShellExecution` hook fires and denies (check `guard-adapter.py` was invoked).
   If the hook does NOT fire in headless `-p` mode, rely on the static `Shell(...)` deny list and
   note the reduced coverage; do not claim the hook is active when it isn't.
5. **Council seats** — write `local/seats.cursor.json`: native = `cursor-agent -p --mode plan` on the
   host's own model (usually Grok); externals = roster minus xAI = {Opus, GPT, GLM, Gemini}. Resolve
   the Grok slug via `cursor-agent --list-models`.
6. **Caveats:** no global CLI instruction file (coverage is per-project `AGENTS.md`); no Stop-event
   hook (council runs via the skill, not an automatic nudge).
