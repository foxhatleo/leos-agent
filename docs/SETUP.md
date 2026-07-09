# Setup — the install interview (agent-facing)

Natural-language runbook. You (the installing agent) drive it conversationally, ask all decisions
up front, and use the `bin/` tools for every mechanical write. Every step has an **already-done**
check (mostly `ls -l` on a symlink), so an interrupted run resumes cleanly. There is no state file
beyond the gitignored `local/`.

## Step 0 — decisions up front

Ask Leo (skip a question if the answer is obvious from the machine):

- **Which hosts** to configure — any of: Claude Code (`~/.claude`), Codex (`~/.codex`), OpenCode
  (`~/.config/opencode`), Cursor (`~/.cursor`). Detect which are installed and default to those.
- **Package manager** for the command allowlist: pnpm / yarn / npm.
- **Council reviewer transports** (per external seat) — see Step 4. Default recommendation: Opus via
  `claude --safe-mode`, GPT via `codex exec`, GLM/Gemini/Grok via OpenCode + OpenRouter (no extra
  CLI). Only offer a transport whose CLI is installed.

## Step 1 — clone location

Confirm the clone is under `$HOME` (required — the tools refuse dests outside HOME). Default
`~/.leos-agent`. For a hardened setup, symlink from a release worktree instead of the dev clone (see
ARCHITECTURE §"release worktree"); for a personal machine the dev clone is fine.

## Step 2 — per host: link → merge → seats → verify

For EACH chosen host `<H>`, read `tools/<H>/SETUP-DELTA.md` and do:

1. **Symlinks:** `python3 bin/leos-link.py --tool <H>`. It creates the links in
   `tools/<H>/linkmap.json` and refuses to clobber a foreign regular file (back it up and re-run
   with `--force` if Leo approves).
2. **Merge fragment(s):** `python3 bin/leos-merge.py --tool <H>` (backs up the dest first). For
   Claude, also append the machine's package-manager allow set from
   `core/policy/policy-data.json.commandAllow.<pm>` into `permissions.allow` (machine-local).
3. **Machine-local config** in `local/` (gitignored): `guard-config.json` (optional
   `{"homeToplevel":[...]}`), `council/config.json` (`{"disabledProjects":[]}`), and the seats file
   from Step 4.
4. **Verify** per the host's SETUP-DELTA (guard blocks `rm -rf ~`; `council.py root` prints the
   clone; the skill is discoverable). Restart the host so hooks load.

## Step 3 — the guard config (optional)

If Leo keeps project roots directly under `$HOME` (e.g. `~/workspace`), add them to
`local/guard-config.json` `homeToplevel` so the guard treats them as home-level (blocks a bare
recursive delete of them) — otherwise the defaults (Desktop/Documents/… ) are enough.

## Step 4 — council seats (asked here, stored gitignored)

For each host, write `local/seats.<host>.json` — this is where the roster is resolved. It is NOT
committed (it holds machine-local transport choices + resolved model slugs).

1. **Native seat** = this host's own model:
   - Claude Code → `{"native":{"mode":"subagent","model":"opus","efforts":{"default":"high","max":"xhigh"}}}`
     (Opus line only — confirm the resolved slug is an Opus id, never Fable/Mythos).
   - Codex → `{"native":{"mode":"exec","transport":"stdin","argv":["codex","exec","--sandbox","read-only","--skip-git-repo-check","-c","model_reasoning_effort={EFFORT}","-"],"efforts":{...}}}`.
   - OpenCode/Cursor → the `--agent plan` / `--mode plan` self-pass with no `-m`.
2. **External seats** = the roster in `core/council/seats.catalog.json` **minus this host's own
   provider**, strongest-first. For each: pick the transport (ask Leo; default per the catalog),
   resolve the provider's CURRENT flagship slug, substitute it into the argv template, and add the
   per-seat `env` (e.g. an isolated `CODEX_HOME` for a codex seat on a non-Codex host —
   create `local/isolated-codex-home/` empty).
3. **Smoke-test every seat** before adding it (each driver in `core/council/drivers/` has the exact
   command). Drop any seat whose smoke test fails; note native-only fallback if none remain.
4. Never commit this file. Re-running setup re-resolves slugs (that's how you refresh models later —
   no committed version goes stale).

## Step 5 — verify everything

Run all five batteries and the doctor; ALL must pass:
```
python3 tests/guard-tests.py && python3 tests/fmt-tests.py && python3 tests/council-tests.py \
  && python3 tests/merge-tests.py && python3 tests/link-tests.py
python3 bin/leos-doctor.py
```
Then a live check per configured host (guard blocks `rm -rf ~`; a trivial council convenes with the
native seat + at least one external seat and does NOT nest). Report what actually ran — never claim
a host is wired if its smoke test didn't pass.
