---
name: install
disable-model-invocation: true
description: Install, update, or remove Leo's global agent preferences in this harness's own instruction file. Run after installing or upgrading the leos-agent plugin.
---

# Install Leo's preferences into this harness

The leos-agent plugin ships its operating policy as one payload. Its skills,
commands, and rules load through each harness's plugin system, but the global
instruction file — the one loaded into every session — has to be written to
disk. This skill does that write.

**It installs only into the harness you are running in.** Do not install the
others: Leo
may be on a different version of the plugin there, and each harness's file is
that harness's business.

## Steps

1. **Identify your harness.** One of: `claude`, `codex`, `cursor`, `hermes`,
   `pi`, `opencode`. Use the one you are actually running in — do not guess from
   the project's files.

2. **Locate the plugin root**, the directory holding `rules/preferences.md`. In
   order of preference: `$LEOS_AGENT_ROOT`, `$CLAUDE_PLUGIN_ROOT`,
   `$PLUGIN_ROOT`, or the nearest ancestor of this file that contains it. The
   script finds it on its own in most cases, so a bare path usually works.

3. **Run the installer**, substituting your harness:

   ```
   python3 <plugin-root>/scripts/leo-install.py <harness>
   ```

4. **Report what it printed** — one line per target. A run that reports
   everything `unchanged` means the preferences were already current; say so
   rather than implying you changed something. Repeat any warning verbatim.

| Status | Meaning |
|---|---|
| `created`, `updated` | the file was written |
| `unchanged` | already current, nothing written |
| `skipped` | not applicable here (Cursor, or a missing Hermes `SOUL.md`) |
| `removed` | uninstall took the block or file out |
| `error` | **the run failed** — exit 1, nothing written for that target |
| `conflict` | **refused** — a file this tool did not write is in the way |

`error` and `conflict` are failures, not progress. Report them as such, quote
the reason the script gave, and do not re-run hoping for a different result. An
`error` on malformed markers means the target file has an unpaired or duplicated
`<leos-agent>` marker: show the user the message and let them fix the file, or
offer to look at it — never edit around it by hand-writing the block yourself.
A `conflict` means something already occupies a path the installer writes to; pass
`--force` only if the user confirms that file should be replaced.

## Other modes

- `--dry-run` shows the diffs and writes nothing. Use it when Leo wants to see
  what would change first, or when a target file has content you did not expect.
- `--uninstall` removes the `<leos-agent>` block and any files this skill
  installed, leaving everything else in those files intact. Run it **before**
  uninstalling the plugin, while the script is still on disk.
- `--check` exits non-zero when the file is out of date, for scripting.

## What it touches

The payload goes into a `<leos-agent>` block. Updating replaces that block and
nothing else, so anything Leo wrote in those files by hand survives. Notes:

- **Hermes**: `~/.hermes/SOUL.md` is edited only if it already exists. Hermes
  writes its own starter identity file on first run; if the installer reports it
  skipped, run Hermes once and install again.
- **Cursor**: nothing is written. Cursor has no on-disk global rules file, and
  the plugin's always-apply rule already delivers the payload.
- **OpenCode**: the skill and command files are copied into
  `~/.config/opencode/skills/` and `~/.config/opencode/commands/`, because
  OpenCode plugins cannot register them from JS. The config file itself is
  never modified.
