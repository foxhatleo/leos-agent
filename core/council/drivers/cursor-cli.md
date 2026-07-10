# Driver: Cursor CLI (`cursor-agent`) — any seat Cursor routes (Grok / Opus / GPT / GLM / Gemini)

`cursor-agent --model <slug>` is model-agnostic: it carries any provider Cursor exposes. So beyond
the **Grok** external seat, it is the **universal fallback transport** for the Opus / GPT / GLM /
Gemini seats whenever their preferred CLI (`claude` / `codex` / `opencode`) isn't installed — a
Cursor-only machine can host the whole council through this one CLI. Also the NATIVE seat when the
host IS Cursor. **Not installed by default** — the setup interview only offers this transport if
`cursor-agent` is on PATH.

**Model:** the flagship slug for whichever provider this seat carries. **Always confirm the exact
slug with `cursor-agent --list-models`** — Cursor's slugs differ from OpenRouter's; an Opus seat
must resolve to an Opus-line id, never Fable/Mythos. If a provider isn't
listed, Cursor can't carry that seat — use another transport. Resolve at setup.

**Install / auth:** install the Cursor CLI (`cursor-agent --version`); auth via Cursor login.

**Seat argv (arg transport):**
```
cursor-agent -p --model {MODEL} --mode plan {PROMPT_TEXT}
```
- `--mode plan` requests a read-only agent. Cursor has no `--safe-mode`; recursion isolation =
  plan mode + run in a clean project dir (Cursor reads `.cursor/rules` / `AGENTS.md` from the
  project), not an absolute OS-level containment guarantee.
- `{PROMPT_TEXT}` replaced in-memory, then shell-quoted.

**Smoke test:**
```
env LEOS_COUNCIL_SEAT=1 cursor-agent -p --model {MODEL} --mode plan 'Reply with the single word OK.'
```
Expect a documented JSON output contract in addition to the answer. **UNCERTAIN (verify before
trusting):** whether this Cursor version supports a structured output flag and reliably runs
headless `-p`/honors `--mode plan`. Add `"adapter":"cursor-json"` **and** the verified JSON
string field, e.g. `"responsePath":"result"`, to the seat only after that smoke test passes;
otherwise the runner refuses to dispatch the seat rather than classifying prose or bookkeeping JSON
as an observable review result. If it hangs or edits, use the OpenCode + OpenRouter route for Grok
instead (the current Grok OpenRouter slug resolved at setup).

**Native use (host IS Cursor):** `cursor-agent -p --mode plan {PROMPT_TEXT}` with no `--model`
(host's own model).
