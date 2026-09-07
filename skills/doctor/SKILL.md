---
name: doctor
disable-model-invocation: true
description: Audit Leo's agent setup in this harness — whether the payload actually reached the session, everything else always loaded into context, and the local plugin checkout. Read-only.
---

# Diagnose Leo's agent setup

Read-only. Report findings; change nothing unless Leo asks.

**This harness only.** Inspect the harness you are actually running in — one of
`claude`, `codex`, `cursor`, `hermes`, `pi`, `opencode`. Other harnesses may be
on other versions; that is their business.

## 1. Injection and install

Locate the plugin root (the directory holding `rules/preferences.md`):
`$LEOS_AGENT_ROOT`, `$CLAUDE_PLUGIN_ROOT`, `$PLUGIN_ROOT`, or the nearest
ancestor of this file that contains it. Confirm
`<plugin-root>/scripts/leo-install.py` actually exists at the resolved root
before running anything — a root that resolves but holds no `scripts/` is
itself a finding (a stale env var, or a copy separated from its plugin).

The payload is no longer rendered into a global file at install time — it is
read live out of the plugin directory every session. Verify it actually
arrived, not that some file was once written:

- **Claude Code and Codex**: confirm `hooks/hooks.json` declares a
  `SessionStart` hook. Then run it exactly as the hook would:

  ```
  python3 <plugin-root>/scripts/emit_payload.py </dev/null
  ```

  It must exit 0 and print non-empty output. Run it twice and diff the two —
  they must be byte-identical. That determinism is the invariant the whole
  design rests on: a byte that varies between runs turns a cached prompt
  prefix into a full cache write every session, so a diff here is a real
  finding, not a nitpick.
- **Cursor**: unchanged from before — the always-apply rule at
  `rules/preferences.md` is read straight from the plugin, no install step.
- **Hermes**: `register(ctx)` calls `ctx.register_system_prompt_section` at
  startup; confirm the section is present in this session's system prompt.
- **Pi**: a JS extension's `before_agent_start` appends the payload; confirm
  it shows up in this session.
- **OpenCode**: confirm the advisory line is actually present in
  `~/.config/opencode/opencode.json`:

  ```
  "instructions": ["<abs plugin root>/rules/preferences.md"]
  ```

  The installer never edits that file — it is JSONC with user comments — so it
  only prints the line and reports it outstanding until someone adds it by
  hand. Its absence is expected until Leo has done that once, not a bug.

Then check for a leftover from the old scheme — any global file an earlier
version of this plugin wrote a `<leos-agent version="...">` block into:
`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.hermes/SOUL.md`,
`~/.pi/agent/AGENTS.md`, `~/.config/opencode/AGENTS.md`. These are no longer
read for the payload — a block surviving in one is dead weight, not a source
of truth — but if one is still there, say to run
`python3 <plugin-root>/scripts/leo-install.py <harness>` to migrate it away.

Also confirm, since none of the above proves the plugin loaded at all:

- The plugin's skills and commands are actually registered in this session —
  `install` and `doctor` should both be listed (on OpenCode the installed copy
  is named `leo-install`, not `install`). If they are not, the plugin is on
  disk but not loaded.
- On OpenCode only: the copied skills under `~/.config/opencode/skills/` are
  installed with the plugin root baked in as an absolute path. Spot-check one —
  the path it names must still exist on disk; a dead path means the plugin
  cache moved and `/leo-install` needs a re-run.

| Harness | How the payload arrives |
|---|---|
| claude | `SessionStart` hook runs `emit_payload.py`; stdout becomes session context |
| codex | same `SessionStart` hook as Claude Code |
| cursor | `rules/preferences.md`, an always-apply rule read straight from the plugin |
| hermes | `register(ctx)` registers a system prompt section at `after_memory` |
| pi | a JS extension's `before_agent_start` appends the payload to the system prompt |
| opencode | an `instructions` line in `~/.config/opencode/opencode.json`, added by hand once |

## 2. Global context

Inventory everything loaded into *every* session in this harness, not just the
leos-agent block. Look for what is broken, stale, or contradictory:

- The global instruction file outside the block — content that fights the
  payload, notes from an older setup, anything referencing files or flags that
  no longer exist.
- Memory files, if the harness has them (Claude: `~/.claude/projects/*/memory/`
  and its `MEMORY.md` index). Flag index lines pointing at missing files,
  memories missing frontmatter, duplicates, and facts that name paths or flags
  that no longer exist. Verify before calling one stale.
- Global settings, agents, skills, commands, and plugins that ship
  always-on instructions. Flag broken JSON, duplicate names, and dangling paths.
- Total size. Codex concatenates the AGENTS.md chain under a byte cap — over
  ~28 KB globally, say so, since repo instructions get crowded out.

## 3. Local checkout

If the plugin root is a git checkout rather than an installed cache, run:

```
python3 <plugin-root>/scripts/check.py
```

Report the failures verbatim. Also note an uncommitted or behind-upstream
checkout — the payload is read live from this checkout every session, so a
dirty or stale one is served immediately, with no version mismatch to flag it.

`leo-install.py <harness> --check` still exists; it no longer covers the
payload, but it still reports whether the remaining installed files (Codex's
`leo-runner.toml`/`leo-executor.toml`, Cursor's routing `.mdc`, OpenCode's
skill and command copies) are current. Run it and quote a non-zero result.

## Report

Group by section, worst first. One line per finding: what is wrong, where, and
the fix. End with a one-line verdict. If everything passes, say so plainly and
do not pad the report.
