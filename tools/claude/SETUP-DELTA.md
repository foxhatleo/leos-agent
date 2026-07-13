# Setup delta — Claude Code

Host-specific steps layered on the shared `docs/SETUP.md` interview.

1. **Detect** `~/.claude` (Claude Code home). Confirm `claude --version`.
2. **Symlinks** — run `bin/leos-python bin/leos-link.py --tool claude` (creates the `links` in
   `tools/claude/linkmap.json` — hooks, council.py, the skill dir; refuses to clobber any foreign
   regular file without asking). Global instructions are NOT a CLAUDE.md symlink — see step 3.
3. **Global instructions (@import block)** — `bin/leos-python bin/leos-block.py --tool claude` ensures a
   marker-delimited `@<clone>/global/AGENTS.md` block in `~/.claude/CLAUDE.md`. It **coexists** with
   any existing CLAUDE.md (never clobbers), is idempotent, and **auto-migrates** a legacy bare
   `CLAUDE.md → clone` symlink to a real file carrying the block. Claude resolves `@import` natively,
   so `git pull` upgrades the imported file live. Verify the block's `@import` points at the clone.
4. **Settings merge** — `bin/leos-python bin/leos-merge.py --tool claude` ownership-merges
   `settings-fragment.json`. Only read-only fixed commands are pre-approved; package scripts and
   mutating Git commands use normal host confirmation.
5. **Council seats** — install through `bin/leos-seats.py` into the unified `seats[]` array (no
   top-level `native`). Claude's own-provider seat is `{"mode":"subagent","model":"opus",...}`
   (Opus line only — never Fable/Mythos; minTier 1). The seven target roles, their transport
   preference (best → fallback), and `minTier` presets: **opus** (subagent on Claude = the
   own-provider seat; or `claude` CLI → cursor → opencode elsewhere, `opus-4.8` /
   `claude-opus-4-8`, minTier 1), **gpt** (codex → cursor → opencode; `gpt-5.6-sol` per the OpenAI
   flavor rule — most capable flavor of the newest GPT generation, minTier 2), **grok** (cursor →
   opencode; `grok-4.5`, minTier 3), **glm** (cursor → opencode; `glm-5.2`, minTier 4), **gemini**
   (cursor → opencode; `gemini-3.1-pro`, minTier 4), **mimo** (opencode only;
   `xiaomi/mimo-v2.5-pro`, minTier 4), **deepseek** (opencode only; `deepseek/deepseek-v4-pro`,
   minTier 4). On a Claude host, the opus seat is `mode: subagent` (in-process, pinned model); all
   others are `mode: exec`. **Best effort:** attempt all seven; install a seat only if its best
   available transport is installed AND its driver smoke passes; silently drop the rest. For any
   `codex` argv, NEVER override `CODEX_HOME` (it throws away host auth — isolation comes from
   `--ephemeral` + scratch cwd + `LEOS_COUNCIL_SEAT`; doctor rejects `env.CODEX_HOME` on codex
   seats). Optional per-seat `envFile` (`local/council/env/<seat>.env`, 0600, gitignored) for
   secrets — inline `env` is non-secret only (secret-named keys refused at install). After each
   exec seat's smoke passes, validate → write (only `mode: exec` seats need `--confirm-smoke`; the
   `mode: subagent` opus seat is not smoke-gated):
   ```sh
   bin/leos-python bin/leos-seats.py validate --host claude --input local/seats-candidate.claude.json
   bin/leos-python bin/leos-seats.py write --host claude --input local/seats-candidate.claude.json \
     --confirm-smoke <exec-seat-1> --confirm-smoke <exec-seat-2>
   ```
   **Migration:** an old-shape `seats.claude.json` (top-level `native`, missing `mode`/`minTier`)
   is rejected by doctor — regenerate via SETUP step 5 + `leos-seats.py write`.
6. **Restart** Claude Code so the hooks load. Verify:
   - `echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf ~"}}' | ~/.claude/leos-python ~/.claude/hooks/bash-guard.py; echo $?` → 43.
   - `~/.claude/leos-python ~/.claude/council/bin/council.py root` prints the clone path.
   - Skill visible: `~/.claude/skills/council/SKILL.md` resolves.
   - Global instructions loaded: `~/.claude/CLAUDE.md` contains the `leos-agent:global-instructions`
     `@import` block, and your own CLAUDE.md content (if any) is intact.
