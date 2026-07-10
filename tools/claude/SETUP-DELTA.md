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
5. **Council seats** — install through `bin/leos-seats.py`: native = a read-only Agent subagent pinned
   to **`model: opus`** (Opus line only — never Fable/Mythos); externals = roster minus Anthropic =
   {GPT, GLM, Gemini, Grok}. Per external seat, pick a transport whose CLI is installed (default:
   GPT→codex, GLM/Gemini/Grok→opencode+OpenRouter; fall back to `cursor-agent` when the preferred
   CLI is absent) and resolve each provider's current flagship slug. Run each seat's driver smoke
   test before adding it. External Claude CLI transports use `--no-session-persistence`.
6. **Restart** Claude Code so the hooks load. Verify:
   - `echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf ~"}}' | ~/.claude/leos-python ~/.claude/hooks/bash-guard.py; echo $?` → 43.
   - `~/.claude/leos-python ~/.claude/council/bin/council.py root` prints the clone path.
   - Skill visible: `~/.claude/skills/council/SKILL.md` resolves.
   - Global instructions loaded: `~/.claude/CLAUDE.md` contains the `leos-agent:global-instructions`
     `@import` block, and your own CLAUDE.md content (if any) is intact.
