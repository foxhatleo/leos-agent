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

**Preconditions (per platform):** a fresh **macOS** ships no `python3` until Xcode Command Line
Tools are installed (`xcode-select --install`, or use Homebrew python / an explicit `LEOS_PYTHON`
path); on **Debian/Ubuntu** the `venv` module ships as a separate package, so `apt install
python3-venv` if `python3 -m venv` fails. **Native Windows is unsupported** — POSIX shell,
symlinks, the `pwd` module, and process-group signals are assumed; use **WSL**.

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
   `{"homeToplevel":[...]}`), `council/config.json` (`{"disabledProjects":[]}`; the optional
   `requireSignoffAtCritical` key defaults to `true` — set it to `false` only when the operator
   wants to drop the critical-tier hard sign-off gate), and the seats file
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
   - Claude Code → `{"mode":"subagent","model":"opus","provider":"anthropic","minTier":2,"efforts":{"default":"high","max":"xhigh"}}`
     (Opus line only — confirm the resolved slug is an Opus id, never Fable/Mythos; `minTier` is 2
     because the own-provider seat is never the sole tier-1 reviewer — the lead-foreign GPT seat
     takes minTier 1). `mode: subagent`
     is an in-process host subagent (Claude Code only — the one harness with a true subagent
     primitive + `--safe-mode`); it is not smoke-gated at install.
   - Codex → `{"mode":"exec","transport":"stdin","provider":"openai","minTier":2,"argv":["codex","exec","--ephemeral","--sandbox","read-only","--skip-git-repo-check","-c","model_reasoning_effort={EFFORT}","-m","{MODEL}","-"],"efforts":{...}}`
     with `{MODEL}` replaced in the machine-local candidate (a runner subprocess reusing the host's
     codex login, same `CODEX_HOME`, no separate key). **NEVER override `CODEX_HOME` on a codex
     seat** — isolation comes from `--ephemeral` + scratch cwd + `LEOS_COUNCIL_SEAT`; doctor rejects
     `env.CODEX_HOME` on codex seats.
   - OpenCode/Cursor → `mode: exec`, `minTier: 2` (own-provider), the `--agent plan` / `--mode plan`
     self-pass with no `-m` (host's own model); the lead-foreign Opus seat takes minTier 1.
2. **The seven target roles** (opus/gpt/glm/gemini/grok/mimo/deepseek) with per-role transport
   preference (best→fallback) from `core/council/seats.catalog.json` `presets.transportPreference`:
   opus = subagent (Claude only) → `claude` → `cursor` → `opencode`; gpt = `codex` → `cursor` →
   `opencode`; grok = `cursor` → `opencode`; glm = `cursor` → `opencode`; gemini = `cursor` →
   `opencode`; mimo = `opencode` only; deepseek = `opencode` only. Current target slugs:
   opus-4.8 / claude-opus-4-8, gpt-5.6-sol, glm-5.2, gemini-3.1-pro, grok-4.5, mimo-v2.5-pro,
   deepseek-v4-pro. **`minTier` (diversity-first):** the own-provider seat is never the sole tier-1
   reviewer — assign it minTier 2 and give minTier 1 to the strongest *reachable foreign* lineage,
   so the everyday (tier-1) review is cross-lineage. Per host: Claude → GPT@1, Opus@2; Codex →
   Opus@1, GPT@2; Cursor/OpenCode → Opus@1, own@2. Remaining foreign seats: grok=3,
   glm/gemini/mimo/deepseek=4. Foreign-strength order for the tier-1 slot: GPT > Grok > Gemini > GLM
   > DeepSeek > MiMo (Opus is strongest foreign on non-Claude hosts). If NO foreign seat is
   reachable at all, give the own-provider seat minTier 1 and warn at the end that diversity is
   reduced (step 6). For each
   role: pick the first transport whose CLI is installed, resolve the provider's CURRENT flagship
   slug for that transport, substitute it into the argv template, and preserve the recipe's required
   `provider` identity (required on every seat, subagent included, so the runner's diversity count
   is reliable). The display `name` does not determine provider policy; `leos-seats.py`
   refuses a missing/unknown provider.
   **OpenAI flavor rule (Leo's standing rule):** use the most capable flavor of the newest GPT
   generation. GPT-5.6 ships three capability flavors — Sol > Terre > Luna (new names in 5.6, not
   lineages) — so 5.6 resolves to Sol, never a lower-capability flavor of the same generation
   (Terre, Luna); a newer GPT generation supersedes 5.6 automatically and its most capable flavor
   is selected. Resolve the exact slug supported by the chosen transport and confirm it in the
   smoke test.
   **Absent vs. broken (important):** attempt all seven. For each role, walk its transport
   preference (native → cursor → opencode): if a transport's CLI is **absent**, skip to the next
   transport; if a role has no installed transport at all, drop that role (best-effort). But if a
   transport's CLI is **present** and its driver smoke **fails** (e.g. unauthenticated, or a dangling
   CLI symlink that broke on an app update), **STOP setup and surface the exact
   failure** — a present-but-broken CLI is almost always unintentional misconfiguration, so it must
   not silently vanish; let the user fix it (re-auth / repoint) or explicitly choose to exclude that
   role, then continue. A Cursor seat is
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
   `timeoutSeconds: 300` base allowance plus the exec-seat per-checkpoint overrides
   `planTimeoutSeconds: 600` and `implTimeoutSeconds: 600` (impl reviews explore the actual diff
   and routinely exceed 300s); neither override applies to the reduced-diversity fallback. A smoke **failure on a
   present CLI halts setup** (see "Absent vs. broken" above) rather than dropping the seat; a
   genuinely **absent** CLI is skipped best-effort. If, after resolving all roles, no *foreign*
   lineage seat is reachable, warn at the end that diversity is reduced and continue
   (`native-only.md`). The runner
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
Then a live check per configured host (guard blocks `rm -rf ~`; a trivial tier-1 council convenes
with the **lead-foreign** seat — a different lineage than the host author — and does NOT nest). Run
CLI seats through `core/council/bin/runner.py`; blank, invalid, timed-out, and nonzero results are
failed seats, not successful reviews. Report what actually ran — never claim a host is wired if its
smoke test didn't pass. **If no foreign-lineage seat was reachable, warn the user explicitly that
diversity is reduced** — the everyday review falls back to the own-provider (same-lineage) seat, and
`result.json` will carry `reducedDiversity: true`; setup still completes.

`git pull` never deletes or imports legacy state at `~/.local/state/leos-agent/council/state`; it
is left untouched. Doctor reports it as migration-available. Only if Leo explicitly asks to retain
that history, run the non-destructive copy (it refuses a nonempty target and leaves the source
unchanged):

```
bin/leos-python core/council/bin/council.py migrate-legacy-state
```

## Troubleshooting — a seat returned no review

When a council reports that a seat "returned nothing," read that seat's `reason` in `result.json`
(also surfaced by the skill); its full CLI output is at the seat's `stderrPath` / `stdoutPath`. The
three causes seen in practice:

- **Timed out.** The seat exceeded its wall-clock budget mid-review (impl reviews of large diffs are
  the usual trigger) and was killed before emitting final findings — so the whole review is lost, and
  a `claude`-CLI seat loses it entirely because it buffers output until the end. Fix: the catalog now
  gives exec seats `implTimeoutSeconds: 600` (was a flat 300); if a very large diff still exceeds it,
  raise that seat's `implTimeoutSeconds`/`timeoutSeconds` (max 900) or review a smaller change.
- **Not authenticated.** The seat's CLI reported e.g. `Not logged in`. Sign the seat's CLI in
  (`claude` / `codex` / `opencode` login) and re-run. A CLI that is present but unauthenticated is a
  misconfiguration to fix, not a seat to drop.
- **Sandbox/permission denial.** The seat CLI was denied access to its home-dir state or credentials
  (`~/.codex` read-only database, `~/.local/share/opencode` log, keychain), typically because the
  council was launched from **inside a sandboxed orchestrator** (e.g. a Codex session running under
  `workspace-write` seatbelt): child seat processes inherit the sandbox and cannot reach anything
  outside the workspace. Run the council from an unsandboxed session so seats can reach their own
  state and auth.
