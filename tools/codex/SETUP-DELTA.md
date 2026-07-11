# Setup delta — Codex CLI

Host-specific steps layered on the shared `docs/SETUP.md` interview.

0. **Preflight** — `codex doctor --summary` (or `codex --version` + a login check). Don't proceed
   if the CLI or auth is broken.
1. **Detect** `$CODEX_HOME` (default `~/.codex`).
2. **Symlinks** — `bin/leos-python bin/leos-link.py --tool codex` links executable payloads and the
   instruction injector. Global instructions are delivered by SessionStart, **not** by a
   `$CODEX_HOME/AGENTS.md` symlink (which would clobber the user's own global instructions). If a
   leftover `$CODEX_HOME/AGENTS.md` clone-symlink exists from an older install, remove it (it's a
   symlink — safe; `leos-doctor` flags it).
3. **Config + hooks merge** — `bin/leos-python bin/leos-merge.py --tool codex` ownership-merges
   `config-fragment.toml` and Leo hook entries into existing host files without replacing user hooks.
4. **Policy** — Codex has no enforced declarative permission surface; `command-policy-notes.json`
   is **advisory** (model guidance), and secret-*reads* are hook/sandbox-mediated only, NOT
   pattern-enforced. Do NOT paste Claude `permissions.allow`/`deny` strings into Codex config.
5. **Council seats** — use `bin/leos-seats.py` as described in shared setup: native = `codex exec
   --ephemeral` read-only pass pinned with `-m` to GPT-5.6 Sol, unless a GPT model with a higher
   numeric version has been released; externals = roster minus OpenAI = {Opus, GLM, Gemini, Grok}. For the
   Opus seat use `claude --safe-mode --no-session-persistence` (Opus line only). Codex external
   seats retain normal authentication and use `--ephemeral`. Resolve slugs and run driver smokes.
6. **Restart** Codex so hooks load, and **trust `hooks.json` once** via `/hooks` (or use
   `--dangerously-bypass-hook-trust` on `codex exec` for automation). NOTE: a later `git pull` that
   changes `hooks.json` may require **re-trusting** it. Verify:
   - `echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf ~"}}' | "${CODEX_HOME:-$HOME/.codex}"/leos-python "${CODEX_HOME:-$HOME/.codex}"/hooks/bash-guard.py; echo $?` → 43.
   - `"${CODEX_HOME:-$HOME/.codex}"/leos-python "${CODEX_HOME:-$HOME/.codex}"/council/bin/council.py root` prints the clone.
   - Injector emits valid JSON: `"${CODEX_HOME:-$HOME/.codex}"/leos-python "${CODEX_HOME:-$HOME/.codex}"/hooks/inject-instructions.py` prints a
     `SessionStart` `additionalContext` object with the global instructions.
   - Global instructions actually reach a session (trust granted): start a Codex session and confirm
     it can quote a rule from `global/AGENTS.md`, while the user's own `~/.codex/AGENTS.md` (if any)
     still loads.
   - Skill discoverable at `~/.agents/skills/council` (verify with the Codex skills list).
