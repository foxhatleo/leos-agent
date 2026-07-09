# Driver: OpenCode (`opencode run`) — GLM / Gemini / Grok seats via OpenRouter

The default transport for **GLM**, **Gemini**, and (optionally) **Grok**. One CLI serves any model
via OpenRouter slugs. Also the NATIVE seat when the host IS OpenCode.

**Models (resolve at setup; OpenRouter slugs):** GLM `z-ai/glm-5.2`, Gemini `google/gemini-3.1-pro`,
Grok `x-ai/grok-4.5`. (Prefer this route for Gemini — the native Gemini-CLI plan mode is
experimental.)

**Install / auth:** `opencode --version`; set `OPENROUTER_API_KEY` (or configure the provider).

**Seat argv (arg transport):**
```
opencode run --agent plan -m openrouter/{MODEL} --variant {EFFORT} {PROMPT_TEXT}
```
- `--agent plan` = OpenCode's read-only agent (edits denied). Recursion isolation = read-only +
  OpenCode's own config (it does not load CLAUDE.md-style council mandates); run from the repo root.
- `{PROMPT_TEXT}` is replaced in-memory with the prompt-file content, then shell-quoted — never
  interpolated into an unquoted fragment.
- efforts (GLM): `{ "default": "high", "max": "max" }`; (Gemini): `{ "default": "xhigh", "max": "max" }`.

**Smoke test (per model):**
```
env LEOS_COUNCIL_SEAT=1 opencode run --agent plan -m openrouter/{MODEL} 'Reply with the single word OK.'
```
Expect `OK` and confirm the seat actually invokes the model (not a placeholder echo).

**Native use (host IS OpenCode):** `opencode run --agent plan {PROMPT_TEXT}` with no `-m` (host's
own model).
