# Setup delta — Claude Code

Host-specific steps layered on the shared `docs/SETUP.md` interview.

1. **Detect** `~/.claude` (Claude Code home). Confirm `claude --version`.
2. **Symlinks** — run `python3 bin/leos-link.py --tool claude` (creates the `links` in
   `tools/claude/linkmap.json`, refusing to clobber any foreign regular file without asking).
3. **Settings merge** — `python3 bin/leos-merge.py --tool claude` merges
   `settings-fragment.json` into `~/.claude/settings.json` (backs it up first). Then **append the
   machine's package-manager allow** (ask which PM: pnpm/yarn/npm) from
   `core/policy/policy-data.json.commandAllow.<pm>` into `permissions.allow` — this part is
   machine-local.
4. **Council seats** — write `local/seats.claude.json`: native = a read-only Agent subagent pinned
   to **`model: opus`** (Opus line only — never Fable/Mythos); externals = roster minus Anthropic =
   {GPT, GLM, Gemini, Grok}. Ask per external seat which transport to use (default: GPT→codex,
   GLM/Gemini/Grok→opencode+OpenRouter) and resolve each provider's current flagship slug. Only
   offer transports whose CLI is installed. Run each seat's driver smoke test before adding it.
5. **Restart** Claude Code so the hooks load. Verify:
   - `echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf ~"}}' | python3 ~/.claude/hooks/bash-guard.py; echo $?` → 43.
   - `python3 ~/.claude/council/bin/council.py root` prints the clone path.
   - Skill visible: `~/.claude/skills/council/SKILL.md` resolves.
