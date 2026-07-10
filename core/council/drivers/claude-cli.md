# Driver: Claude CLI (`claude`) — Anthropic / Opus seat

Serves the **Opus** external seat (when the host is NOT Claude Code). Recursion isolation here is
first-class: `--safe-mode`.

**Model:** the Opus line only — `--model opus` (alias tracks the latest Opus). NEVER Fable or
Mythos (the Claude-5 / Mythos-class line). At setup, confirm the resolved model is an Opus id.

**Install / auth:** `claude --version` (Claude Code). Auth via the normal Claude Code login.

**Seat argv (stdin transport):**
```
claude --safe-mode --print --permission-mode plan --model opus --effort {EFFORT}
```
- `--safe-mode` disables CLAUDE.md, skills, plugins, hooks, MCP, custom agents/commands — this is
  the mechanical guarantee the seat cannot convene its own council. Do **not** drop it.
- `--permission-mode plan` = read-only (no edits).
- efforts: `{ "default": "high", "max": "xhigh" }`.

**Smoke test (must pass before adding the seat):**
```
printf 'Reply with the single word OK.' | env LEOS_COUNCIL_SEAT=1 claude --safe-mode --print \
  --permission-mode plan --model opus --output-format json
```
Expect one valid JSON response. The runner adds `--output-format json` when absent and records an
empty/malformed response as a failed seat; a seat may use ordinary subagents but not Leo's council.

**Native use:** when the host IS Claude Code, Opus is the NATIVE seat instead — a read-only Agent
subagent pinned to `model: opus` (not this CLI). See SKILL.md Seats model.
