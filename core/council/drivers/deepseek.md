# Driver: OpenCode (`opencode run`) — DeepSeek seat

Serves the **DeepSeek** seat (provider `deepseek`). **OpenCode+OpenRouter ONLY** — `cursor-agent`
is excluded due to a known `deepseek-v4-pro` incompatibility on Cursor: the `reasoning_content`
replay breaks long tool chains, and Cursor caps context at 200K while opencode passes the full 1M.
Dropped entirely if opencode is not installed or its driver smoke fails. `minTier` preset 4
(critical-only by default).

**Model:** DeepSeek flagship — current target `deepseek-v4-pro`. The OpenRouter slug is
`deepseek/deepseek-v4-pro`. Resolve at setup; never commit it.

**Install / auth:** `opencode --version`; set `OPENROUTER_API_KEY`.

**Seat argv (arg transport):**
```
opencode run --agent plan -m openrouter/deepseek/deepseek-v4-pro --variant {EFFORT} {PROMPT_TEXT}
```
- `--agent plan` requests OpenCode's plan-policy mode — not an OS-level read-only containment
  guarantee. Recursion isolation uses the runner sentinel (`LEOS_COUNCIL_SEAT=1`) + OpenCode's own
  config (it does not load CLAUDE.md-style council mandates) + a runner-provided scratch cwd with a
  synthetic Git root so the reviewed project's local agent config never loads, including during
  self-review; the repo's absolute path arrives in a prompt header.
- `{PROMPT_TEXT}` is replaced in-memory with the prompt-file content and passed as a single argv
  element — direct argv execution (no shell), never interpolated into an unquoted shell fragment.
- efforts: `{ "default": "xhigh", "max": "max" }`.

**Per-seat env (optional):** non-secret inline values go in the seat's `env` dict (secret-named
keys refused at install). For the OpenRouter key, prefer a per-seat **envFile** at
`local/council/env/deepseek.env` (mode 0600, gitignored; secret-named keys ARE allowed there). The
runner loads it at dispatch and its contents never enter prompts, logs, or `result.json`. Enforcing
hosts deny the LLM reading `**/council/env/**`.

**Smoke test (must pass before adding the seat):**
```
env LEOS_COUNCIL_SEAT=1 opencode run --agent plan --format json -m openrouter/deepseek/deepseek-v4-pro 'Reply with the single word OK.'
```
Use `--format json` in the smoke test and expect valid JSON; the runner adds it when absent. Confirm
the seat actually invokes the model (not a placeholder echo). If the smoke fails or opencode is
absent, drop this seat — DeepSeek has no alternate transport (cursor is intentionally excluded).

**Session privacy:** the supported CLI contract has no verified non-persistence switch. External
dispatch therefore requires explicit project-send approval, and setup discloses possible retained
provider session metadata.
