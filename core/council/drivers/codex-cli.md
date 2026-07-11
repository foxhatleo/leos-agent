# Driver: Codex CLI (`codex exec`) — OpenAI / GPT seat

Serves the **GPT** external seat (when the host is NOT Codex). Also the NATIVE seat when the host
IS Codex.

**Model:** the most capable flavor of the newest GPT generation (Leo's standing rule). GPT-5.6
ships three capability flavors — Sol > Terre > Luna (new names in 5.6, not lineages) — so 5.6
resolves to Sol, never a lower-capability flavor of the same generation (Terre, Luna). A newer GPT
generation supersedes 5.6 automatically; select its most capable flavor. Resolve the exact `-m`
slug supported by this CLI at setup and store it only in `local/seats.<host>.json`.

**Install / auth:** `codex --version`; auth via the normal Codex login.

**Seat argv (stdin transport):**
```
codex exec --ephemeral --sandbox read-only --skip-git-repo-check -c model_reasoning_effort={EFFORT} -m {MODEL} -
```
- `--sandbox read-only` (default for `codex exec`) = no writes.
- Codex has no `--safe-mode`. It retains normal `CODEX_HOME` authentication; `--ephemeral` prevents
  session-file persistence, the inherited `LEOS_COUNCIL_SEAT=1` suppresses council hooks, and the
  runner refuses any nested council attempt.
- The runner launches the seat in an empty scratch cwd (not a git repo), which is why
  `--skip-git-repo-check` stays mandatory in the argv; the reviewed repo's absolute path arrives
  in a prompt header and the read-only sandbox can still read it by path.
- efforts: `{ "default": "high", "max": "xhigh" }`.

**Smoke test:**
```
env LEOS_COUNCIL_SEAT=1 codex exec --ephemeral --sandbox read-only \
  --skip-git-repo-check --json -m {MODEL} - <<<'Reply with the single word OK.'
```
Expect JSONL events with a final response. Never use `codex review` (its output contract differs
from the findings JSON); the runner classifies missing/invalid JSONL explicitly.

**Native use (host IS Codex):** use the same pinned `-m {MODEL}` argv. The setup-time OpenAI
flavor rule applies to both native and external Codex seats, so the native reviewer may differ
from the host session's own model. (Exception: native-only fallback mode has no resolved
`{MODEL}` — see `native-only.md`.)
