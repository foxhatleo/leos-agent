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
5. **Council seats** — install through `bin/leos-seats.py`: native = `cursor-agent -p --mode plan` on the
   host's own model (usually Grok); externals = roster minus xAI = {Opus, GPT, GLM, Gemini}. Resolve
   the Grok slug via `cursor-agent --list-models`. For the external GPT seat, apply the OpenAI
   flavor rule (most capable flavor of the newest GPT generation: 5.6 → Sol, never Terre/Luna) and
   confirm the exact Cursor slug with `cursor-agent --list-models`.
6. **Caveats:** no verified global CLI instruction file (coverage is per-project `AGENTS.md`); no
   Stop-event hook (council runs via the skill, not an automatic nudge). No session-persistence-off
   flag is assumed; disclose this before approving Cursor as an external transport.
