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

Native plugin installation enables discovery; this helper maintains artifacts
that need machine-specific paths or models. It does not install other harnesses
or silently approve hook trust. What it does per harness:

- Codex: install the cheap, standard, premium, parent, and reviewer agent
  TOMLs; remove a legacy global policy block. Review hook trust in `/hooks`
  afterward. Rerun after upgrading the plugin.
- Cursor: install native user agent profiles; remove the old global routing
  rule that stock Cursor did not load. The plugin supplies its native rule.
  Rerun after changing tier mappings.
- OpenCode: register the plugin and one rendered instruction in the active
  JSON/JSONC config while preserving comments/unrelated entries; install native
  agents and skill/reference copies; remove unchanged old command wrappers.
  Rerun after a source/cache path changes, then restart OpenCode.
- Claude, Hermes and Pi: only remove a policy block an older release left in
  the global instruction file. Their plugin or extension supplies everything
  else, so on a machine without such a block there is nothing to do.

All three file-writing harnesses also remove unchanged copies of the retired
`leo-runner` and `leo-executor` profiles. The harness config directory must
already exist; the helper never creates one.

The helper stages and validates changes before applying them, backs up affected
files under `~/.leos-agent-local/install-backups`, and restores earlier writes
on a handled failure. It honors native config-directory overrides. A file is
ours only while its bytes match the install receipt or a known release copy:
an edited copy or a foreign file is a conflict (a failed install, not a partial
upgrade), and an edited pre-v12 copy is reported `preserved` while the rest
continues.

Modes:

- `--dry-run`: preview with diffs; writes nothing.
- `--check`: read-only; nonzero if artifacts differ or a check fails. It cannot
  prove the plugin is active in a running session.
- `--rollback`: undo the last install or uninstall only where current bytes
  still match the applied or original state. Refuse intervening user edits.
- `--uninstall`: remove owned artifacts and registrations; files that are not
  provably ours are `preserved`, even with `--force`. Keep routing config,
  handoffs, and user data. Run before removing the native plugin/source.

Report changed, preserved, and failed targets accurately. Use `--force` only for
a specific conflicting file the user explicitly wants replaced. Never work
around a conflict by directly overwriting the file. Successful install/update
starts a bounded background price refresh unless LEOS_AGENT_PRICE_REFRESH=off;
a network failure preserves the last catalog and does not invalidate installed
files.
