---
description: Diagnose Leo's agent setup in this harness — injection, global context, and local checkout.
---

Run the leos-agent diagnosis for **this harness only**. Read-only; change
nothing unless asked.

Determine which harness you are running in (`claude`, `codex`, `cursor`,
`hermes`, `pi`, or `opencode`), locate the plugin root — the directory
containing `rules/preferences.md`, available as `$LEOS_AGENT_ROOT`,
`$CLAUDE_PLUGIN_ROOT`, or `$PLUGIN_ROOT` on most harnesses — and follow the
`doctor` skill:

1. `python3 <plugin-root>/scripts/leo-install.py <harness> --check`, plus a read
   of the harness's global file to confirm one current `<leos-agent>` block.
2. Audit the rest of this harness's always-loaded context: the global
   instruction file outside the block, memory files and their index, global
   settings, skills, commands, and plugins.
3. If the plugin root is a git checkout, `python3 <plugin-root>/scripts/check.py`.

Report findings grouped by section, worst first, one line each, then a one-line
verdict.
