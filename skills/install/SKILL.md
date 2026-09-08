---
name: install
disable-model-invocation: true
description: Install or upgrade leos-agent's native profiles and owned configuration entries for this harness, preserving unrelated settings. Supports preview, check, uninstall, and rollback.
---

# Install leos-agent's native integration

Use only the harness the user requested, or the current harness if unspecified:
claude, codex, cursor, hermes, pi, or opencode. Resolve the absolute plugin root
from `LEOS_AGENT_ROOT`, `CLAUDE_PLUGIN_ROOT`, `PLUGIN_ROOT`, or the nearest
ancestor containing `rules/preferences.md`.

```
python3 "<plugin-root>/scripts/leo-install.py" <harness>
```

Run after installing/upgrading the native plugin or changing tier mappings.
Native plugin installation enables discovery; this helper maintains artifacts
that need machine-specific paths or models. It does not install other harnesses
or silently approve hook trust.

- Claude: remove legacy policy blocks; native plugin ships agents and hooks.
- Codex: install cheap, standard, parent, reviewer, and legacy alias TOMLs;
  remove a legacy global policy block. Review hook trust in `/hooks` afterward.
- Cursor: install native user agent profiles; remove the old global routing
  rule that stock Cursor did not load. The plugin supplies its native rule.
- OpenCode: register the plugin and one rendered instruction in the active
  JSON/JSONC config while preserving comments/unrelated entries; install native
  agents and deferred skill/reference copies; remove unchanged old command
  wrappers. Rerun after a source/cache path changes.
- Hermes and Pi: migrate legacy policy blocks. Native plugin/extension loading
  supplies the policy and skills; do not assume per-dispatch model selection.

The helper stages and validates changes before applying them, backs up affected
files locally, and restores earlier writes on a handled failure. It honors
native config-directory environment overrides. Unrelated files and changed
legacy copies are preserved. A conflict or error is a failed install, not a
successful partial upgrade.

Modes:

- `--dry-run`: preview; writes nothing.
- `--check`: read-only; nonzero if artifacts differ or a check fails. It cannot
  prove the plugin is active in a running session.
- `--rollback`: restore the last backup only where current bytes still match
  the applied or original state. Refuse intervening user edits.
- `--uninstall`: remove owned artifacts and registrations; keep routing config,
  handoffs, and user data. Run before removing the native plugin/source.

Report changed targets and failures accurately. Use `--force` only for a
specific conflicting file the user explicitly wants replaced. Never work around
a conflict by directly overwriting the file. Successful install/update starts a
bounded background price refresh unless LEOS_AGENT_PRICE_REFRESH=off; a network
failure preserves the last catalog and does not invalidate installed files.
