# Setup delta — Codex CLI

Host-specific steps layered on the shared `docs/SETUP.md` interview.

0. **Preflight** — `codex doctor --summary` (or `codex --version` + a login check). Don't proceed
   if the CLI or auth is broken.
1. **Detect** `$CODEX_HOME` (default `~/.codex`).
2. **Symlinks** — `python3 bin/leos-link.py --tool codex` (links per `tools/codex/linkmap.json`,
   including the whole-file `~/.codex/hooks.json` symlink).
3. **Config merge** — `python3 bin/leos-merge.py --tool codex` merges `config-fragment.toml`
   (enables `[features] hooks`) into `~/.codex/config.toml` (round-trip checked, backed up first).
4. **Policy** — Codex has no enforced declarative permission surface; `command-policy-notes.json`
   is **advisory** (model guidance), and secret-*reads* are hook/sandbox-mediated only, NOT
   pattern-enforced. Do NOT paste Claude `permissions.allow`/`deny` strings into Codex config.
5. **Council seats** — write `local/seats.codex.json`: native = `codex exec` read-only pass on the
   host's own model (no `-m`); externals = roster minus OpenAI = {Opus, GLM, Gemini, Grok}. For the
   Opus seat use `claude --safe-mode` (Opus line only). Create `local/isolated-codex-home/` (empty)
   only if a *codex* external seat is ever configured on a non-Codex host. Resolve current slugs at
   setup; run driver smoke tests.
6. **Restart** Codex so hooks load (Codex may need `--dangerously-bypass-hook-trust` on `codex exec`
   or a one-time trust of `hooks.json` — confirm hooks fire). Verify:
   - `echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf ~"}}' | python3 ~/.codex/hooks/bash-guard.py; echo $?` → 43.
   - `python3 ~/.codex/council/bin/council.py root` prints the clone.
   - Skill discoverable at `~/.agents/skills/council` (verify with the Codex skills list).
