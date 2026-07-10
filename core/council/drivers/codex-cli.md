# Driver: Codex CLI (`codex exec`) — OpenAI / GPT seat

Serves the **GPT** external seat (when the host is NOT Codex). Also the NATIVE seat when the host
IS Codex.

**Model:** the latest Codex/GPT flagship. Resolve its `-m` slug at setup and store it only in
`local/seats.<host>.json`.

**Install / auth:** `codex --version`; auth via the normal Codex login.

**Seat argv (stdin transport):**
```
codex exec --ephemeral --sandbox read-only --skip-git-repo-check -c model_reasoning_effort={EFFORT} -m {MODEL} -
```
- `--sandbox read-only` (default for `codex exec`) = no writes.
- Codex has no `--safe-mode`. It retains normal `CODEX_HOME` authentication; `--ephemeral` prevents
  session-file persistence, the inherited `LEOS_COUNCIL_SEAT=1` suppresses council hooks, and the
  runner refuses any nested council attempt.
- efforts: `{ "default": "high", "max": "xhigh" }`.

**Smoke test:**
```
env LEOS_COUNCIL_SEAT=1 codex exec --ephemeral --sandbox read-only \
  --skip-git-repo-check --json -m {MODEL} - <<<'Reply with the single word OK.'
```
Expect JSONL events with a final response. Never use `codex review` (its output contract differs
from the findings JSON); the runner classifies missing/invalid JSONL explicitly.

**Native use (host IS Codex):** same argv without `-m` (uses the host's own model).
