# Setup — the install interview (agent-facing)

Natural-language runbook. You (the installing agent) drive it conversationally, ask all decisions
up front, and use the `bin/` tools for every mechanical write. Every step has an **already-done**
check (mostly `ls -l` on a symlink), so an interrupted run resumes cleanly. There is no tracked
state: all Leo-owned config, runtime, results, backups, and temporary files live under gitignored
`local/`.

## Step 0 — decisions up front

Ask Leo (skip a question if the answer is obvious from the machine):

- **Which hosts** to configure — any of: Claude Code (`~/.claude`), Codex (`~/.codex`), OpenCode
  (`~/.config/opencode`), Cursor (`~/.cursor`). **Default to only the host you (the installing
  agent) are running on** — a Claude Code session sets up `claude`, a Codex session `codex`, an
  OpenCode session `opencode`, a Cursor session `cursor`. Detect the *other* installed hosts and
  **offer** them, but configure another host only if Leo explicitly asks for it. (Identify your own
  host from your runtime; if you genuinely can't tell, ask Leo rather than defaulting to all.)
- **Council reviewer transports** (per seat) — see Step 5. Default recommendation: Opus via
  `claude --safe-mode` (or `mode: subagent` on a Claude host), GPT via `codex exec`, GLM/Gemini/Grok
  via OpenCode + OpenRouter. But a seat is **available whenever ANY installed transport can reach
  its provider** — `cursor-agent` can also carry Opus/GPT/GLM/Gemini/Grok, so on a machine with
  Cursor but not OpenCode, GLM/Gemini resolve via `cursor-agent`. MiMo and DeepSeek are
  OpenCode+OpenRouter only. Enumerate each role's transport preference (`presets.transportPreference`
  in the catalog), pick the first installed one whose smoke passes, and ask Leo only when more than
  one is available. Drop a seat **only** if none of its transports have an installed CLI.

## Step 1 — clone location

Confirm the clone is under `$HOME` (required — the tools refuse dests outside HOME). Default
`~/.leos-agent`. For a hardened setup, symlink from a release worktree instead of the dev clone (see
ARCHITECTURE §"release worktree"); for a personal machine the dev clone is fine.

## Step 2 — create the private Python runtime

Leo's Agents supports **CPython 3.9+** on current macOS (including Tahoe+) and mainstream Linux
distributions. The bootstrap interpreter is used only to create the clone-private runtime; no
system package manager is run and no global packages are installed.

```
python3 bin/leos-runtime.py setup
bin/leos-python -c 'import sys; print(sys.executable)'
```

If ambient `python3` is older than 3.9 or lacks `venv`, ask Leo for an approved CPython path and
use `LEOS_PYTHON=/path/to/python3.11 python3 bin/leos-runtime.py setup` (or `--python`). Setup
creates `local/.venv` and installs the hash-locked TOML dependency in `requirements/runtime.txt`.
Every subsequent Leo command below uses `bin/leos-python`; do not activate the venv or rely on the
interpreter a host happens to find.

Configure Git's global excludes for the repository-local council controls. This uses the existing
`core.excludesFile` when one is configured; otherwise it creates `~/.gitignore` and configures Git
to use it. It backs up an existing file before appending `.council-off` and `.council.json`, and is
idempotent:

```
bin/leos-python bin/leos-gitignore.py
```

## Step 3 — per host: link → merge → seats → verify

For EACH chosen host `<H>`, read `tools/<H>/SETUP-DELTA.md` and do:

1. **Symlinks:** `bin/leos-python bin/leos-link.py --tool <H>`. It creates the links in
   `tools/<H>/linkmap.json` and refuses to clobber a foreign regular file (back it up and re-run
   with `--force` if Leo approves).
2. **Merge fragment(s):** `bin/leos-python bin/leos-merge.py --tool <H>` (backs up the dest first,
   preserves unrelated TOML comments/order, and records ownership for safe upgrades/uninstall).
   Codex and Cursor hook registries are merges, not whole-file symlinks. For OpenCode this merge
   also writes the `instructions[]` global-instruction reference (the
   `{{CLONE_ROOT}}` token is expanded to the clone path).
3. **Global instructions (additive — never clobber):** delivered per host. **Claude** →
   `bin/leos-python bin/leos-block.py --tool claude` (the managed `@import` block; auto-migrates a legacy
   CLAUDE.md symlink). **Codex** → the `SessionStart` injector linked in step 1 (trust `hooks.json`
   once via `/hooks`). **OpenCode** → the `instructions[]` entry from step 2. **Cursor** → none
   (per-project only). If an older install left a `~/.codex/AGENTS.md` or
   `~/.config/opencode/AGENTS.md` clone-symlink, remove it (safe — it's a symlink; `leos-doctor`
   flags it).
4. **Machine-local config** in `local/` (gitignored): `guard-config.json` (optional
   `{"homeToplevel":[...]}`), `council/config.json` (`{"disabledProjects":[]}`), and the seats file
   from Step 5.
5. **Verify** per the host's SETUP-DELTA (guard blocks `rm -rf ~`; `council.py root` prints the
   clone; the skill is discoverable; global instructions load without clobbering the user's own).
   Restart the host so hooks load.

`leos-link` records the host in `local/installed-hosts.json`; doctor checks this explicit registry,
not every pre-existing host directory. Existing installations without that registry are recognized
read-only from their Leo links/seats/merge records.

## Step 4 — the guard config (optional)

If Leo keeps project roots directly under `$HOME` (e.g. `~/workspace`), add them to
`local/guard-config.json` `homeToplevel` so the guard treats them as home-level (blocks a bare
recursive delete of them) — otherwise the defaults (Desktop/Documents/… ) are enough.

Formatting/lint tools can execute repository binaries and configuration plugins, so the edit hook
is inert until Leo explicitly trusts a canonical project root. Write only approved roots to:

```json
{"projects":["/absolute/path/to/project"]}
```

in `local/format-trust.json`. Never trust a project merely because it is below `$HOME`. Rust edits
run `rustfmt` only; the hook never runs `cargo clippy`, builds, package scripts, or lifecycle hooks.

## Step 5 — council seats (asked here, stored gitignored)

For each host, prepare a candidate JSON under `local/`, then install it only through
`bin/leos-seats.py`. The destination `local/seats.<host>.json` is mode 0600 and gitignored. The
candidate is a single unified `seats[]` array — **no top-level `native` object**. Every reviewer,
including the host's own-provider seat, is one element with `mode` in {`subagent`, `exec`} (the old
`mode: native` is gone), a `minTier` (integer 1..4, default 4; a seat runs at council tier T iff
`seat.minTier <= T`), an optional inline `env` (non-secret only — secret-named keys
TOKEN/SECRET/PASSWORD/API_KEY are refused at install), and an optional `envFile` (the per-seat
secret channel).

1. **Own-provider seat** = this host's provider, as one element of `seats[]`:
   - Claude Code → `{"mode":"subagent","model":"opus","minTier":1,"efforts":{"default":"high","max":"xhigh"}}`
     (Opus line only — confirm the resolved slug is an Opus id, never Fable/Mythos). `mode: subagent`
     is an in-process host subagent (Claude Code only — the one harness with a true subagent
     primitive + `--safe-mode`); it is not smoke-gated at install.
   - Codex → `{"mode":"exec","transport":"stdin","minTier":2,"argv":["codex","exec","--ephemeral","--sandbox","read-only","--skip-git-repo-check","-c","model_reasoning_effort={EFFORT}","-m","{MODEL}","-"],"efforts":{...}}`
     with `{MODEL}` replaced in the machine-local candidate (a runner subprocess reusing the host's
     codex login, same `CODEX_HOME`, no separate key). **NEVER override `CODEX_HOME` on a codex
     seat** — isolation comes from `--ephemeral` + scratch cwd + `LEOS_COUNCIL_SEAT`; doctor rejects
     `env.CODEX_HOME` on codex seats.
   - OpenCode/Cursor → `mode: exec`, the `--agent plan` / `--mode plan` self-pass with no `-m`
     (host's own model).
2. **The seven target roles** (opus/gpt/glm/gemini/grok/mimo/deepseek) with per-role transport
   preference (best→fallback) from `core/council/seats.catalog.json` `presets.transportPreference`:
   opus = subagent (Claude only) → `claude` → `cursor` → `opencode`; gpt = `codex` → `cursor` →
   `opencode`; grok = `cursor` → `opencode`; glm = `cursor` → `opencode`; gemini = `cursor` →
   `opencode`; mimo = `opencode` only; deepseek = `opencode` only. Current target slugs:
   opus-4.8 / claude-opus-4-8, gpt-5.6-sol, glm-5.2, gemini-3.1-pro, grok-4.5, mimo-v2.5-pro,
   deepseek-v4-pro. `minTier` presets: opus=1, gpt=2, grok=3, glm/gemini/mimo/deepseek=4. For each
   role: pick the first transport whose CLI is installed, resolve the provider's CURRENT flagship
   slug for that transport, substitute it into the argv template, and preserve the recipe's required
   `provider` identity. The display `name` does not determine provider policy; `leos-seats.py`
   refuses a missing/unknown provider.
   **OpenAI flavor rule (Leo's standing rule):** use the most capable flavor of the newest GPT
   generation. GPT-5.6 ships three capability flavors — Sol > Terre > Luna (new names in 5.6, not
   lineages) — so 5.6 resolves to Sol, never a lower-capability flavor of the same generation
   (Terre, Luna); a newer GPT generation supersedes 5.6 automatically and its most capable flavor
   is selected. Resolve the exact slug supported by the chosen transport and confirm it in the
   smoke test.
   **Best-effort seat dropping:** attempt all seven; install a seat only if its best available
   transport is installed AND its driver smoke passes; silently drop the rest. A Cursor seat is
   added only after its smoke test proves a JSON output contract; record `"adapter":"cursor-json"`
   and its verified nonempty string `"responsePath"` in that seat.
3. **Per-seat env (secret channel).** Add only non-secret per-seat environment values to the inline
   `env` dict (secret-named keys are refused). For secrets (an API key the CLI does not already pick
   up from host login — e.g. `OPENROUTER_API_KEY`), use a per-seat **envFile** at
   `local/council/env/<seat>.env` (mode 0600, gitignored; secret-named keys ARE allowed in it). The
   runner loads and merges it into the seat subprocess env at dispatch; its contents never appear in
   prompts, logs, or `result.json`. Enforcing hosts deny the LLM reading `**/council/env/**` via
   policy. The env-file parser is hand-rolled (no python-dotenv dependency).
4. **Smoke-test every `mode: exec` seat** before adding it (each driver in
   `core/council/drivers/` has the exact command — `claude-cli.md`, `codex-cli.md`, `opencode.md`,
   `cursor-cli.md`, `mimo.md`, `deepseek.md`). Require the structured-output form for
   Claude/Codex/OpenCode. `mode: subagent` seats are NOT smoke-gated. Preserve the catalog's
   `timeoutSeconds: 300` implementation allowance and `planTimeoutSeconds: 600` exec-seat plan
   allowance; the latter never applies to the reduced-diversity fallback. Drop any exec seat whose
   smoke test fails; note reduced-diversity fallback (`native-only.md`) if none remain. The runner
   is invoked only by an orchestrator review decision; it never starts a council by itself. Every
   exec runner dispatch also requires explicit `--approve-external` project-send acknowledgement.
5. Validate, then atomically write after every exec-seat smoke test passed:
   ```sh
   bin/leos-python bin/leos-seats.py validate --host <H> --input local/seats-candidate.<H>.json
   bin/leos-python bin/leos-seats.py write --host <H> --input local/seats-candidate.<H>.json \
     --confirm-smoke <seat1> --confirm-smoke <seat2>
   ```
   Repeat `--confirm-smoke` for every `mode: exec` seat; `mode: subagent` seats need none.
6. Never commit this file. Re-running setup re-resolves slugs (that's how you refresh models later —
   no committed version goes stale). **Migration:** an old-shape seats file (top-level `native`,
   missing `mode`/`minTier`) is rejected by `leos-doctor.py` after `git pull` — regenerate via this
   step + `leos-seats.py write`. `setup --refresh` is a no-op (no new deps for the redesign).

## Step 6 — verify everything

Run all batteries and the doctor; ALL must pass:
```
bin/leos-python tests/guard-tests.py && bin/leos-python tests/fmt-tests.py \
  && bin/leos-python tests/council-tests.py && bin/leos-python tests/runner-tests.py \
  && bin/leos-python tests/gitignore-tests.py \
  && bin/leos-python tests/merge-tests.py && bin/leos-python tests/link-tests.py \
  && bin/leos-python tests/block-tests.py && bin/leos-python tests/inject-tests.py \
  && bin/leos-python tests/uninstall-tests.py && bin/leos-python tests/runtime-tests.py \
  && bin/leos-python tests/seats-tests.py \
  && bin/leos-python tests/contamination-check.py && bin/leos-python bin/leos-render-policy.py --check
bin/leos-python bin/leos-doctor.py
```
Then a live check per configured host (guard blocks `rm -rf ~`; a trivial council convenes with the
own-provider seat + at least one foreign-provider seat and does NOT nest). Run CLI seats through
`core/council/bin/runner.py`; blank, invalid, timed-out, and nonzero results are failed seats, not
successful reviews. Report what actually ran — never claim a host is wired if its smoke test didn't
pass.

`git pull` never deletes or imports legacy state at `~/.local/state/leos-agent/council/state`; it
is left untouched. Doctor reports it as migration-available. Only if Leo explicitly asks to retain
that history, run the non-destructive copy (it refuses a nonempty target and leaves the source
unchanged):

```
bin/leos-python core/council/bin/council.py migrate-legacy-state
```
