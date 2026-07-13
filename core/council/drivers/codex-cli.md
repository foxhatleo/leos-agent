# Driver: Codex CLI (`codex exec`) — OpenAI / GPT seat

Serves the **GPT** seat on a **non-Codex** host (`mode: exec`), and is also the host's own-provider
GPT seat **on a Codex host** (`mode: exec` — a runner subprocess reusing the host's codex login,
same `CODEX_HOME`, no separate key). Either way it is `mode: exec`; there is no `mode: native`.

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
- Codex has no `--safe-mode`. **NEVER override `CODEX_HOME` on a codex seat** — the old catalog did
  this for isolation and it threw away host auth. It RETAINS normal `CODEX_HOME` authentication;
  isolation comes from `--ephemeral` (no session-file persistence) + a runner-provided scratch cwd
  with a synthetic empty Git root + the inherited `LEOS_COUNCIL_SEAT=1` sentinel (suppresses
  council hooks; the runner refuses any nested council). Doctor rejects `env.CODEX_HOME` on a codex
  seat.
- `--skip-git-repo-check` stays in the portable argv; the reviewed repo's absolute path arrives in a
  prompt header and the read-only sandbox can still read it by path.
- efforts: `{ "default": "high", "max": "xhigh" }`.

**Per-seat env (optional):** non-secret inline values go in the seat's `env` dict (secret-named
keys refused at install). For a separate API key, use a per-seat **envFile** at
`local/council/env/gpt.env` (mode 0600, gitignored; secret-named keys ARE allowed there). The runner
loads it at dispatch and its contents never enter prompts, logs, or `result.json`. Enforcing hosts
deny the LLM reading `**/council/env/**`. Do **not** put `CODEX_HOME` in either — see above.

**Smoke test:**
```
env LEOS_COUNCIL_SEAT=1 codex exec --ephemeral --sandbox read-only \
  --skip-git-repo-check --json -m {MODEL} - <<<'Reply with the single word OK.'
```
Expect JSONL events with a final response. Never use `codex review` (its output contract differs
from the findings JSON); the runner classifies missing/invalid JSONL explicitly.

**Own-provider use (host IS Codex):** use the same pinned `-m {MODEL}` argv — it is a `mode: exec`
seat in `seats[]`, not a separate native block. The setup-time OpenAI flavor rule applies to both
own-provider and foreign Codex seats, so the own-provider reviewer may differ from the host
session's own model. (Exception: reduced-diversity fallback mode has no resolved `{MODEL}` — see
`native-only.md`.)
