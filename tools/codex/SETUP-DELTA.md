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
5. **Council seats** — install through `bin/leos-seats.py` into the unified `seats[]` array (no
   top-level `native`). Codex's own-provider seat is `mode: exec` — `codex exec --ephemeral
   --sandbox read-only` pinned with `-m` per the OpenAI flavor rule (most capable flavor of the
   newest GPT generation: 5.6 → Sol, never Terre/Luna; minTier 2). The seven target roles, their
   transport preference (best → fallback), and `minTier` presets: **opus** (subagent on Claude
   only; on Codex = `mode: exec` via `claude --safe-mode --no-session-persistence`, `opus-4.8` /
   `claude-opus-4-8`, minTier 1), **gpt** (codex → cursor → opencode; on Codex = the own-provider
   seat, `gpt-5.6-sol`, minTier 2), **grok** (cursor → opencode; `grok-4.5`, minTier 3), **glm**
   (cursor → opencode; `glm-5.2`, minTier 4), **gemini** (cursor → opencode; `gemini-3.1-pro`,
   minTier 4), **mimo** (opencode only; `xiaomi/mimo-v2.5-pro`, minTier 4), **deepseek** (opencode
   only; `deepseek/deepseek-v4-pro`, minTier 4). On a Codex host every seat is `mode: exec`
   (Codex has no subagent primitive). **NEVER override `CODEX_HOME` on a codex seat** — the old
   catalog did this for isolation and it threw away host auth. The codex seat RETAINS normal
   `CODEX_HOME`; isolation comes from `--ephemeral` + scratch cwd + `LEOS_COUNCIL_SEAT` sentinel;
   doctor rejects `env.CODEX_HOME` on codex seats. **Best effort:** install a seat only if its
   best available transport is installed AND its driver smoke passes; silently drop the rest.
   Optional per-seat `envFile` (`local/council/env/<seat>.env`, 0600, gitignored) for secrets —
   inline `env` is non-secret only. After each exec seat's smoke passes, validate → write (on
   Codex all seats are `mode: exec`, so all need `--confirm-smoke`):
   ```sh
   bin/leos-python bin/leos-seats.py validate --host codex --input local/seats-candidate.codex.json
   bin/leos-python bin/leos-seats.py write --host codex --input local/seats-candidate.codex.json \
     --confirm-smoke <exec-seat-1> --confirm-smoke <exec-seat-2>
   ```
   **Migration:** an old-shape `seats.codex.json` (top-level `native`, isolated `CODEX_HOME`,
   missing `mode`/`minTier`) is rejected by doctor — regenerate via SETUP step 5 + `leos-seats.py
   write`. This also sweeps any lingering isolated-`CODEX_HOME` codex seats.
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
