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
- **Council reviewer transports** (per external seat) — see Step 5. Default recommendation: Opus via
  `claude --safe-mode`, GPT via `codex exec`, GLM/Gemini/Grok via OpenCode + OpenRouter. But a seat
  is **available whenever ANY installed transport can reach its provider** — `cursor-agent` can also
  carry Opus/GPT/GLM/Gemini/Grok, so on a machine with Cursor but not OpenCode, GLM/Gemini resolve
  via `cursor-agent`. Enumerate each seat's transport variants (`asExternal*` in the catalog), pick
  an installed one, and ask Leo only when more than one is available. Drop a seat **only** if none
  of its transports have an installed CLI.

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
`bin/leos-seats.py`. The destination `local/seats.<host>.json` is mode 0600 and gitignored.

1. **Native seat** = this host's provider:
   - Claude Code → `{"native":{"mode":"subagent","model":"opus","efforts":{"default":"high","max":"xhigh"}}}`
     (Opus line only — confirm the resolved slug is an Opus id, never Fable/Mythos).
   - Codex → resolve the OpenAI model using the flavor rule below, then
     `{"native":{"mode":"exec","transport":"stdin","argv":["codex","exec","--ephemeral","--sandbox","read-only","--skip-git-repo-check","-c","model_reasoning_effort={EFFORT}","-m","{MODEL}","-"],"efforts":{...}}}`
     with `{MODEL}` replaced in the machine-local candidate.
   - OpenCode/Cursor → the `--agent plan` / `--mode plan` self-pass with no `-m` (host's own model).
2. **External seats** = the roster in `core/council/seats.catalog.json` **minus this host's own
   provider**, strongest-first. For each: enumerate its transport variants (`asExternal`,
   `asExternalCursor`, `asExternalOpencode`), pick one whose CLI is installed (ask Leo only when
   more than one is available), resolve the provider's CURRENT flagship slug for that transport,
   substitute it into the argv template, preserve the recipe's required `provider` identity, and
   add only non-secret per-seat environment values. The display `name` does not determine provider
   policy; `leos-seats.py` refuses a missing/unknown provider.
   **OpenAI flavor rule (Leo's standing rule):** use the most capable flavor of the newest GPT
   generation. GPT-5.6 ships three capability flavors — Sol > Terre > Luna (new names in 5.6, not
   lineages) — so 5.6 resolves to Sol, never a lower-capability flavor of the same generation
   (Terre, Luna); a newer GPT generation supersedes 5.6 automatically and its most capable flavor
   is selected. Resolve the exact slug supported by the chosen transport and confirm it in the
   smoke test.
   Codex seats retain normal `CODEX_HOME` authentication and use `--ephemeral`; recursion is
   mechanically refused by `LEOS_COUNCIL_SEAT`. A seat is dropped only if NONE of its transports
   have an installed CLI. The runner recognizes Claude,
   Codex, and OpenCode adapters automatically. A Cursor seat is added only after its smoke test
   proves a JSON output contract; record `"adapter":"cursor-json"` and its verified nonempty
   string `"responsePath"` in that seat.
3. **Smoke-test every seat** before adding it (each driver in `core/council/drivers/` has the exact
   command). Require the structured-output form for Claude/Codex/OpenCode. Preserve the catalog's
   `timeoutSeconds: 300` implementation allowance and `planTimeoutSeconds: 600` external flagship
   plan allowance; the latter never applies to native fallback. Drop any seat whose
   smoke test fails; note native-only fallback if none remain. The runner is invoked only by an
   orchestrator review decision; it never starts a council by itself.
   Claude and Codex transports must use their non-persistence flags. The documented OpenCode and
   Cursor transports do not expose an equivalent verified flag; disclose that limitation when
   offering them. Every external runner dispatch also requires explicit `--approve-external`
   project-send acknowledgement.
4. Validate, then atomically write after every external smoke test passed:
   ```sh
   bin/leos-python bin/leos-seats.py validate --host <H> --input local/seats-candidate.<H>.json
   bin/leos-python bin/leos-seats.py write --host <H> --input local/seats-candidate.<H>.json \
     --confirm-smoke <seat1> --confirm-smoke <seat2>
   ```
   Repeat `--confirm-smoke` for every external seat; native-only configs need none.
5. Never commit this file. Re-running setup re-resolves slugs (that's how you refresh models later —
   no committed version goes stale).

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
native seat + at least one external seat and does NOT nest). Run CLI seats through
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
