# Driver: OpenCode (`opencode run`) — GLM / Gemini / Grok / MiMo / DeepSeek (and Opus/GPT fallback) via OpenRouter

The default transport for **GLM**, **Gemini**, **Grok** (fallback), and the **only** transport for
**MiMo** and **DeepSeek**. One CLI serves any model via OpenRouter slugs. Also the last-resort
fallback transport for **Opus** and **GPT** (via OpenRouter) when neither their preferred CLI nor
`cursor-agent` is available. And it is the host's own-provider seat (`mode: exec`) when the host IS
OpenCode.

**Alternate:** if OpenCode isn't installed, GLM / Gemini / Grok can run via `cursor-agent` instead
(see `cursor-cli.md`) — the setup interview picks whichever transport's CLI is present. MiMo and
DeepSeek have no cursor route (see `mimo.md` / `deepseek.md`).

**Models:** resolve the current OpenRouter slug for each role at setup and store it only in
`local/seats.<host>.json`. Prefer this route for Gemini; the native Gemini-CLI plan mode is
experimental.

| Role | OpenRouter slug (current target) |
|---|---|
| GLM | `zhipu/{MODEL}` (glm-5.2) |
| Gemini | `google/{MODEL}` (gemini-3.1-pro) |
| Grok | `xai/{MODEL}` (grok-4.5) |
| MiMo | `xiaomi/mimo-v2.5-pro` |
| DeepSeek | `deepseek/deepseek-v4-pro` |

**Install / auth:** `opencode --version`; set `OPENROUTER_API_KEY` (or configure the provider).

**Seat argv (arg transport):**
```
opencode run --agent plan -m openrouter/{MODEL} --variant {EFFORT} {PROMPT_TEXT}
```
- `--agent plan` requests OpenCode's plan-policy mode. Verify the installed version's actual
  edit/shell behavior during setup; it is not an OS-level read-only containment guarantee.
  Recursion isolation also uses the runner sentinel and OpenCode's own config (it does not load
  CLAUDE.md-style council mandates). The runner launches the seat in a scratch cwd with its own
  synthetic Git root by default so the reviewed project's local agent config never loads, including
  during self-review; the repo's absolute path arrives in a prompt header. Set seat `"cwd": "repo"`
  only if the installed version cannot read outside its cwd (re-opens repo-local instruction
  injection).
- `{PROMPT_TEXT}` is replaced in-memory with the prompt-file content and passed as a single argv
  element — the runner uses direct argv execution (no shell), so it is never interpolated into an
  unquoted shell fragment.
- efforts (GLM): `{ "default": "high", "max": "max" }`; (Gemini/MiMo/DeepSeek): `{ "default": "xhigh", "max": "max" }`.

**Smoke test (per model):**
```
env LEOS_COUNCIL_SEAT=1 opencode run --agent plan --format json -m openrouter/{MODEL} 'Reply with the single word OK.'
```
Use `--format json` in the smoke test and expect valid JSON; the runner adds it when absent. Confirm
the seat actually invokes the model (not a placeholder echo). `--agent plan` is policy mode, not an
absolute OS-level containment guarantee.

**Own-provider use (host IS OpenCode):** `opencode run --agent plan {PROMPT_TEXT}` with no `-m`
(host's own model) — a `mode: exec` seat in `seats[]`.

**Session privacy:** the supported CLI contract has no verified non-persistence switch. External
dispatch therefore requires explicit project-send approval, and setup discloses possible retained
provider session metadata.
