---
name: doctor
disable-model-invocation: true
description: Audit Leo's agent setup in this harness — the injected leos-agent block, everything else always loaded into context, and the local plugin checkout. Read-only.
---

# Diagnose Leo's agent setup

Read-only. Report findings; change nothing unless Leo asks.

**This harness only.** Inspect the harness you are actually running in — one of
`claude`, `codex`, `cursor`, `hermes`, `pi`, `opencode`. Other harnesses may be
on other versions; that is their business.

## 1. Injection and install

Locate the plugin root (the directory holding `rules/preferences.md`):
`$LEOS_AGENT_ROOT`, `$CLAUDE_PLUGIN_ROOT`, `$PLUGIN_ROOT`, or the parent of the
directory holding this file. Then:

```
python3 <plugin-root>/scripts/leo-install.py <harness> --check
```

Exit 0 means the `<leos-agent>` block is present and current. Non-zero means it
is missing, stale, or the file is malformed — quote what it printed and offer
`/leo-install`. Cursor legitimately reports `skipped`; Hermes skips until
`~/.hermes/SOUL.md` exists.

Then confirm by hand, since `--check` only sees disk, not what got loaded:

- Read the harness's global file and verify exactly one `<leos-agent
  version="...">` block, with the version matching `package.json` in the plugin
  root.
- Confirm the plugin's skills and commands are actually registered in this
  session — `install` and `doctor` should both be listed. If they are not, the
  plugin is on disk but not loaded.

| Harness | Global file |
|---|---|
| claude | `~/.claude/CLAUDE.md` |
| codex | `~/.codex/AGENTS.md` (plus `~/.codex/agents/leo-runner.toml` and `leo-executor.toml`) |
| cursor | none — the always-apply rule carries the payload |
| hermes | `~/.hermes/SOUL.md` |
| pi | `~/.pi/agent/AGENTS.md` |
| opencode | `~/.config/opencode/AGENTS.md` (plus copied `skills/`, `commands/`) |

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
checkout, and a `package.json` version that disagrees with the installed
block — a same-version reinstall serves the cached build, so a version match
with different content stays invisible here.

## Report

Group by section, worst first. One line per finding: what is wrong, where, and
the fix. End with a one-line verdict. If everything passes, say so plainly and
do not pad the report.
