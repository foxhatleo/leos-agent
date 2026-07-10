# Setup delta — Codex CLI

Host-specific steps layered on the shared `docs/SETUP.md` interview.

0. **Preflight** — `codex doctor --summary` (or `codex --version` + a login check). Don't proceed
   if the CLI or auth is broken.
1. **Detect** `$CODEX_HOME` (default `~/.codex`).
2. **Symlinks** — `python3 bin/leos-link.py --tool codex` (links per `tools/codex/linkmap.json`,
   including the whole-file `~/.codex/hooks.json` symlink and the `hooks/inject-instructions.py`
   injector). Global instructions are delivered by that SessionStart injector, **not** by a
   `~/.codex/AGENTS.md` symlink (which would clobber the user's own global instructions). If a
   leftover `~/.codex/AGENTS.md` clone-symlink exists from an older install, remove it (it's a
   symlink — safe; `leos-doctor` flags it).
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
6. **Restart** Codex so hooks load, and **trust `hooks.json` once** via `/hooks` (or use
   `--dangerously-bypass-hook-trust` on `codex exec` for automation). NOTE: a later `git pull` that
   changes `hooks.json` may require **re-trusting** it. Verify:
   - `echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf ~"}}' | python3 ~/.codex/hooks/bash-guard.py; echo $?` → 43.
   - `python3 ~/.codex/council/bin/council.py root` prints the clone.
   - Injector emits valid JSON: `python3 ~/.codex/hooks/inject-instructions.py` prints a
     `SessionStart` `additionalContext` object with the global instructions.
   - Global instructions actually reach a session (trust granted): start a Codex session and confirm
     it can quote a rule from `global/AGENTS.md`, while the user's own `~/.codex/AGENTS.md` (if any)
     still loads.
   - Skill discoverable at `~/.agents/skills/council` (verify with the Codex skills list).
