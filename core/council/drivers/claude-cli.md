# Driver: Claude CLI (`claude`) — Anthropic / Opus seat

Serves the **Opus** seat on a **non-Claude** host as a `mode: exec` runner subprocess via the
`claude` CLI. (On a Claude Code host the Opus seat is `mode: subagent` instead — an in-process
read-only Agent subagent pinned to `model: opus`, dispatched by the orchestrator and folded back
via `runner.py collect-subagent`; the `collect-native` alias is kept for back-compat.) Recursion
isolation here is first-class: `--safe-mode`.

**Model:** the Opus line only — `--model opus` (alias tracks the latest Opus). NEVER Fable or
Mythos (the Claude-5 / Mythos-class line). At setup, confirm the resolved model is an Opus id.

**Install / auth:** `claude --version` (Claude Code). Auth via the normal Claude Code login.

**Seat argv (stdin transport):**
```
claude --safe-mode --print --no-session-persistence --permission-mode plan --model opus --effort {EFFORT}
```
- `--safe-mode` disables CLAUDE.md, skills, plugins, hooks, MCP, custom agents/commands — this is
  the mechanical guarantee the seat cannot convene its own council. Do **not** drop it.
- `--no-session-persistence` ensures the seat leaves no resumable session behind (the catalog and
  DESIGN.md both include this flag).
- `--permission-mode plan` = read-only (no edits).
- efforts: `{ "default": "high", "max": "xhigh" }`.

**Per-seat env (optional):** non-secret inline values go in the seat's `env` dict (secret-named
keys — TOKEN/SECRET/PASSWORD/API_KEY — are refused at install). For secret auth (an API key the
CLI does not already pick up from host login), use a per-seat **envFile** at
`local/council/env/opus.env` (mode 0600, gitignored; secret-named keys ARE allowed there). The
runner loads it at dispatch and its contents never enter prompts, logs, or `result.json`.
Enforcing hosts deny the LLM reading `**/council/env/**` via policy.

**Smoke test (must pass before adding the seat):**
```
printf 'Reply with the single word OK.' | env LEOS_COUNCIL_SEAT=1 claude --safe-mode --print \
  --permission-mode plan --model opus --output-format json
```
Expect one valid JSON response. The runner adds `--output-format json` when absent and records an
empty/malformed response as a failed seat; a seat may use ordinary subagents but not Leo's council.

**Subagent mode (Claude Code host):** the Opus seat is `{"mode":"subagent","model":"opus",...}` in
`seats[]`, not this CLI. The runner reports it as `orchestrator-subagent-required`; the orchestrator
dispatches it (it is the one harness with a true subagent primitive + `--safe-mode`) and folds the
result back. A `mode: subagent` seat is not smoke-gated (no `--confirm-smoke` needed at install).

