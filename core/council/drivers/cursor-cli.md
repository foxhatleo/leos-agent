# Driver: Cursor CLI (`cursor-agent`) — any seat Cursor routes (Grok / Opus / GPT / GLM / Gemini)

`cursor-agent --model <slug>` is model-agnostic: it carries any provider Cursor exposes. So beyond
the **Grok** external seat, it is the **universal fallback transport** for the Opus / GPT / GLM /
Gemini seats whenever their preferred CLI (`claude` / `codex` / `opencode`) isn't installed — a
Cursor-only machine can host the whole council through this one CLI. Also the NATIVE seat when the
host IS Cursor. **Not installed by default** — the setup interview only offers this transport if
`cursor-agent` is on PATH.

**Model:** the flagship slug for whichever provider this seat carries. **Always confirm the exact
slug with `cursor-agent --list-models`** — Cursor's slugs differ from OpenRouter's (e.g. Grok
`grok-4.5`; an Opus seat must resolve to an Opus-line id, never Fable/Mythos). If a provider isn't
listed, Cursor can't carry that seat — use another transport. Resolve at setup.

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
