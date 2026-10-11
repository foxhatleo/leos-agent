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
  TOMLs and the read-only review lens; remove a legacy global policy block.
  Review hook trust in `/hooks` afterward. Rerun after upgrading the plugin.
- Cursor: install the same six native user agent profiles; remove the old
  global routing rule that stock Cursor did not load. The plugin supplies its
  native rule. Rerun after changing tier mappings.
- OpenCode: register the plugin and one rendered instruction in the active
  JSON/JSONC config while preserving comments/unrelated entries; install the
  six native agents and skill/reference copies; remove unchanged old command
  wrappers. The instruction is a rendered copy of the policy: rerun after an
  upgrade, a routing change, or a source/cache path change, then restart
  OpenCode. OpenCode 2 loads neither that instruction nor this plugin's hooks;
  only the agents and skills apply there.
- Claude, Hermes and Pi: only remove a policy block an older release left in
  the global instruction file. Their plugin or extension supplies everything
  else, so on a machine without such a block there is nothing to do.
- Claude `--statusline`, only when the user asks for it: set `statusLine` in
  `settings.json` to the cost status line (model, session cost, children by
  tier with reference cost). Other settings stay as they are; a different
  `statusLine` is a conflict unless `--force <settings.json>` names the file,
  and uninstall or rollback puts it back. Rerun it after a plugin upgrade, which
  moves the path.

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
- `--check`: read-only; exit 1 if artifacts differ or a check fails, 3 if
  only preserved copies keep an older payload. It cannot prove the plugin is
  active in a running session.
- `--rollback`: undo the last install or uninstall only where current bytes
  still match the applied or original state. Refuse intervening user edits.
- `--uninstall`: remove owned artifacts and registrations; files that are not
  provably ours are `preserved`, even with `--force`. Keep routing config,
  handoffs, and user data. Run before removing the native plugin/source.

Report changed, preserved, and failed targets accurately. Use `--force <path>`
only for a specific conflicting or preserved file the user explicitly wants
replaced; a bare `--force` replaces every conflict. Never work
around a conflict by directly overwriting the file. Successful install/update
starts a bounded background price refresh unless LEOS_AGENT_PRICE_REFRESH=off;
a network failure preserves the last catalog and does not invalidate installed
files.
