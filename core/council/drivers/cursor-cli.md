# Driver: Cursor CLI (`cursor-agent`) — xAI / Grok seat

Serves the **Grok** external seat via Cursor (when you want Grok through the Cursor app rather than
OpenRouter). Also the NATIVE seat when the host IS Cursor. **Not installed by default** — the setup
interview only offers this transport if `cursor-agent` is on PATH.

**Model:** the latest Grok flagship. Confirm the exact slug with `cursor-agent --list-models`
(expected `grok-4.5`). Resolve at setup.

**Install / auth:** install the Cursor CLI (`cursor-agent --version`); auth via Cursor login.

**Seat argv (arg transport):**
```
cursor-agent -p --model {MODEL} --mode plan {PROMPT_TEXT}
```
- `--mode plan` = read-only (no edits). Cursor has no `--safe-mode`; recursion isolation = read-only
  + run in a clean project dir (Cursor reads `.cursor/rules` / `AGENTS.md` from the project).
- `{PROMPT_TEXT}` replaced in-memory, then shell-quoted.

**Smoke test:**
```
env LEOS_COUNCIL_SEAT=1 cursor-agent -p --model {MODEL} --mode plan 'Reply with the single word OK.'
```
Expect `OK`. **UNCERTAIN (verify before trusting):** whether `cursor-agent` reliably runs headless
`-p` and honors `--mode plan` in this version — if the smoke test hangs or edits, use the OpenCode
+ OpenRouter route for Grok instead (`openrouter/x-ai/grok-4.5`).

**Native use (host IS Cursor):** `cursor-agent -p --mode plan {PROMPT_TEXT}` with no `--model`
(host's own model).
