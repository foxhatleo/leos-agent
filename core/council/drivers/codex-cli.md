# Driver: Codex CLI (`codex exec`) — OpenAI / GPT seat

Serves the **GPT** external seat (when the host is NOT Codex). Also the NATIVE seat when the host
IS Codex.

**Model:** the latest Codex/GPT flagship (`-m gpt-5.5`, then `gpt-5.6`, …). Resolve at setup.

**Install / auth:** `codex --version`; auth via the normal Codex login.

**Seat argv (stdin transport):**
```
codex exec --sandbox read-only --skip-git-repo-check -c model_reasoning_effort={EFFORT} -m {MODEL} -
```
- `--sandbox read-only` (default for `codex exec`) = no writes.
- Codex has **no `--safe-mode`**. Recursion isolation = run the seat with an **isolated neutral
  `CODEX_HOME`** (an empty dir with no `hooks.json`, no `skills/`, no global `AGENTS.md`), passed via
  the seat's `env` map. Setup creates it at `local/isolated-codex-home/` (gitignored). Without this,
  a codex seat could inherit the machine's council Stop hook.
- efforts: `{ "default": "high", "max": "xhigh" }`.

**Smoke test:**
```
env LEOS_COUNCIL_SEAT=1 CODEX_HOME=<clone>/local/isolated-codex-home \
  codex exec --sandbox read-only --skip-git-repo-check -m {MODEL} - <<<'Reply with the single word OK.'
```
Expect `OK`. Never use `codex review` (its output contract differs from the findings JSON).

**Native use (host IS Codex):** same argv without `-m` (uses the host's own model) and without the
isolated `CODEX_HOME` env (it is already the host).
