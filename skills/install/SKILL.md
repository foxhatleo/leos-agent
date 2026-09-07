---
name: install
disable-model-invocation: true
description: Install what this harness's plugin system cannot deliver itself — Codex's agent TOMLs, Cursor's routing rule, OpenCode's copies — and migrate away any legacy leos-agent block. Run after installing or upgrading the plugin.
---

# Install what this harness's plugin system cannot deliver

Every harness now reads Leo's operating policy live out of the plugin
directory each session — Claude Code and Codex through a `SessionStart` hook
running `scripts/emit_payload.py`, Cursor through its always-apply rule,
Hermes through `register_system_prompt_section`, Pi through a
`before_agent_start` extension, OpenCode through an `instructions` line in
`opencode.json`. Upgrading the plugin upgrades the policy; no write to a global
file makes that happen anymore.

This skill has two jobs instead: **migrate** — strip any `<leos-agent
version="...">` block an earlier version of this plugin left in a global file,
leaving the surrounding text intact — and **install** what a harness's plugin
system genuinely cannot deliver by itself: Codex's `leo-runner.toml` and
`leo-executor.toml`, Cursor's routing `.mdc` (when routing is configured),
and OpenCode's copied `skills/` and `commands/` plus the advisory for its
`opencode.json` line, which the installer prints but never writes — that file
is JSONC with user comments, and editing it blind would risk one.

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

4. **Report what it printed** — one line per target. Every harness gets a
   target for its former global file (migration only, see below) plus
   whatever that harness still needs installed. A run that reports everything
   `unchanged` means it was already current; say so rather than implying you
   changed something. Repeat any warning verbatim.

| Status | Meaning |
|---|---|
| `created`, `updated` | an installed file was written — a Codex agent TOML, Cursor's routing rule, an OpenCode copy |
| `migrated` | a legacy `<leos-agent>` block was found in the former global file and stripped; the rest of the file is untouched |
| `unchanged` | already current, nothing written — including a global file with no legacy block, which is the normal, permanent state now |
| `skipped` | not applicable here — Cursor with no routing configured, or OpenCode's `opencode.json` still needs its `instructions` line added by hand |
| `removed` | `--uninstall` took a file out, or a normal run took back a stale Cursor rule after routing was unset |
| `error` | **the run failed** — exit 1, nothing written for that target |
| `conflict` | **refused** — a file this tool did not write is in the way |

`error` and `conflict` are failures, not progress. Report them as such, quote
the reason the script gave, and do not re-run hoping for a different result. An
`error` on malformed markers means the former global file has an unpaired or
duplicated `<leos-agent>` marker: show the user the message and let them fix
the file, or offer to look at it — never edit around it by hand-writing
anything into that file yourself. A `conflict` means something already
occupies a path the installer writes to; pass `--force` only if the user
confirms that file should be replaced.

## Other modes

- `--dry-run` shows the diffs and writes nothing. Use it when Leo wants to see
  what would change first, or when a target file has content you did not expect.
- `--uninstall` removes any legacy `<leos-agent>` block and any files this
  skill installed, leaving everything else in those files intact. Run it
  **before** uninstalling the plugin, while the script is still on disk.
- `--check` exits non-zero when an installed file is out of date, for
  scripting. It does not cover the payload itself — that is read live every
  session, so there is nothing on disk for it to be stale against.

## What it touches

Nothing here delivers the payload; it arrives live from the plugin, per
harness, as described above. This tool's actual writes:

- **Every harness**: its former global file is checked for a leftover
  `<leos-agent>` block and, if one is found, stripped — nothing else in that
  file is touched.
- **Codex**: `~/.codex/agents/leo-runner.toml` and `leo-executor.toml`, with
  this machine's routing config substituted in where one is set.
- **Cursor**: `~/.cursor/rules/leos-agent-routing.mdc`, written only when
  routing is actually configured for Cursor — an unconfigured rule would just
  restate the plugin's default, an always-loaded no-op.
- **Hermes**: nothing beyond the migration above. `~/.hermes/SOUL.md` is no
  longer written at all; Hermes gets the payload through
  `register_system_prompt_section`.
- **Pi**: nothing beyond the migration above.
- **OpenCode**: the skill and command files are copied into
  `~/.config/opencode/skills/` and `~/.config/opencode/commands/`, because
  OpenCode plugins cannot register them from JS. `~/.config/opencode/opencode.json`
  is never edited — it is JSONC with user comments — so a `skipped` result
  there prints the `instructions` line to add by hand.
