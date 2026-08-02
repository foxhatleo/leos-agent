---
name: setup
description: >
  Turn on the opt-in wiring a plugin install cannot turn on for itself.
  Every harness installs Leo through its own plugin system, and none of them
  offers an install-time hook, so anything that writes into a file the user
  already owns is asked for once, here, and recorded in machine-local state.
  Idempotent, but not generally reversible: running it twice changes nothing
  the second time, while removal is harness-specific and manual. Use when Leo
  explicitly requests opt-in setup after installation. Do not use for
  diagnosis or unprompted writes outside the repository.
when_to_use: >
  Leo asks to enable Hermes memory projection, or invokes setup by name after
  installing on a new machine. NOT for diagnosing whether the plugin loaded
  (that is leo:doctor, which reports and never changes anything), and NOT
  something to run unprompted — it writes to a file outside the repository.
---

# setup

Leo's Agent installs through each harness's own plugin system. None of those
systems runs arbitrary code at install time — Hermes' `register()` fires at
session start, not on `hermes plugins install` — so there is no moment during
installation at which consent for a write outside the plugin could be implied.
Anything with that blast radius lives behind this command instead.

## Run it

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py"
```

`${CLAUDE_PLUGIN_ROOT}` is the Claude Code spelling of the plugin root; Codex
exports `$PLUGIN_ROOT`, Cursor `$CURSOR_PLUGIN_ROOT`, and on Hermes and
OpenCode no such variable exists — read the absolute payload path from the
injected policy's `state.py` or `memory.py` command, which was substituted
before injection.

With no arguments it reports what is on, what is available, and what each
feature would actually do right now. It changes nothing. Add `--json` for the
same facts as data.

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" enable hermes-memory
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" disable hermes-memory
```

Enabling something already on prints that and exits 0 — re-running is always
safe, and never a reason to check first.

## `apply`: bootstrap this harness's MCP servers

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" apply
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" apply --dry-run
```

Idempotent, and read-modify-write: `config/models.json`'s `mcp.core` list names
the servers each harness gets, `apply` detects which harness is actually
running this script (the same detection `doctor.py` already does —
never a second, divergent copy of it) and writes only that harness's own
config, never another one's, even when another harness's config file also
exists on the machine. `--dry-run` prints the exact commands or diffs and
touches nothing. An unsupported or undetectable harness refuses outright —
nothing is touched, and the exit code is non-zero.

"Already installed" is always answered by re-reading the harness's own
config, never by a flag Leo remembers — so removing a server by hand and
re-running `apply` sees the removal, and running `apply` twice in a row
writes nothing the second time either way. OpenCode edits are lossless JSONC
additions: comments, trailing commas, indentation, symlinks and modes survive;
existing `opencode.jsonc` and `opencode.json` together make setup refuse rather
than choose. `OPENCODE_CONFIG` wins, otherwise the sole existing global file
wins, otherwise a new `.jsonc` is used.

`apply` is idempotent, **not generally reversible**. It never removes an MCP
server or tool gate. Remove an automatic CLI registration with the command for
the harness that owns it:

```sh
claude mcp remove <name> --scope user
codex mcp remove <name>
hermes mcp remove <name>
```

Cursor has no setup-owned removal command: remove the `mcpServers.<name>` key
from `~/.cursor/mcp.json`. OpenCode likewise has no MCP removal command: remove
the `mcp.<name>` key from the resolved `opencode.jsonc` or `opencode.json`, and
remove any unwanted setup-owned tool gates there. A `.leo-backup` is one
pre-first-write snapshot, not a conflict-aware restore, transaction log, or
promise that a later user edit can be undone.

`apply` also reports (never flips) two Codex toggles: its `computer_use`
feature flag and `web_search` mode (offering, never forcing, the upgrade to
`"live"`), plus one manual Claude in Chrome toggle, which has no config key.

Vendor connectors (Slack, Sentry, Linear, ...) live in the same `mcp` config
under `connectors` — `apply` never installs those; that is the next section.
Core executable packages are exact reviewed pins. Their maintainer update
procedure is recorded in `config/MCP_PINS.md`; never substitute `@latest`, a
range, or an unqualified package name during setup.

## Vendor connectors: `connectors` and `connect`

`config/models.json`'s `mcp.connectors` names eleven vendor MCP servers
(Slack, Sentry, Honeycomb, Snowflake, LaunchDarkly, Linear, Jira +
Confluence, Gmail, Google Drive, Granola, Vercel) — every one OAuth, every
one remote HTTP. Unlike `apply`'s core servers, these are never installed
without a name chosen explicitly. After `apply`, run:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" connectors --json
```

Read-only, and it exits 0 even on a harness `apply` would refuse — it just
has nothing installable to report. Each entry's `installed` is answered by
re-reading the harness's own config the same way `apply` does — matching the
endpoint URL first, a name second, because a claude.ai connector such as
Gmail or Vercel is registered against the account and never written to
`~/.claude.json` at all. Never offer one already `installed: true`.

For every connector still `installed: false`, see the *Structured question
to the user* row of your mapping:

- **A question tool** (Claude Code, OpenCode): present the not-installed
  connectors as a multi-select, one entry per `label`, and install only what
  is chosen:

  ```sh
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" connect <key> [<key> ...]
  ```

- **No question tool** (Codex, Cursor, Hermes): list them in plain text —
  `key`, `label`, and `authNote` — and **install nothing** unless the user
  names one or more by key in reply. The same default as everywhere else in
  this skill: asking is never itself consent.

`snowflake` always needs `needsUrl: true` handled first — its endpoint
embeds org, account, database and schema, and cannot be guessed from
anything on the machine. Ask for the account-specific URL before offering it
in the multi-select (or before accepting it in a plain-text reply), then:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" connect snowflake --url <URL>
```

`connect` refuses outright, writing nothing, on an unsupported harness, an
unknown registry state, or on `snowflake` with no URL on hand. Slack, Gmail,
Google Drive, and account-specific providers are manual-only: report their
prerequisites and never attempt dynamic registration. On Hermes, providers
that support dynamic registration use `hermes mcp add <key> --url <url> --auth oauth`
followed by `hermes mcp login <key>`; providers without it remain manual.
Every successful install reports
`needs-auth`: setup registers the endpoint and stops there — it never
handles a credential. Report back to the user, by `label`, which connectors
now need them to complete a browser OAuth flow on first use.

## Features

### `hermes-memory`

Projects the **global** memory facts into `$HERMES_HOME/SOUL.md` (default
`~/.hermes/SOUL.md`), the same marker-spliced block the other four harnesses
receive in their own per-user file.

It is the one projection target that is opt-in, because it is the one whose
file is not simply a place for user instructions: `SOUL.md` is the agent's
identity prompt and the opening section of every Hermes system prompt on that
machine. The safeguards are the same as everywhere else, plus one:

- Only global-scope facts. Repo facts never leave the store — every per-user
  file loads in every repository, so projecting them would leak one project's
  memories into unrelated sessions.
- Everything outside Leo's `BEGIN`/`END` markers is preserved byte for byte,
  and one `SOUL.md.leo-backup` is taken before the first ever write.
- Unbalanced or duplicated markers abort the write and report an error rather
  than guessing which block is Leo's.
- **The file is never created.** Hermes falls back to a built-in persona when
  `SOUL.md` is absent, so creating it would silently replace the user's agent
  identity. Enabled with no `SOUL.md` present reports `skipped:no-soul` and
  does nothing. Write the file yourself and the next session splices into it.

`LEOS_AGENT_NO_PROJECT=1` still disables all projection, including this one.

Hermes' `memories/MEMORY.md` and `memories/USER.md` are deliberately **not**
targets: the agent owns those through its own memory tool and would overwrite
Leo's markers.

## What setup never does

It does not install, update, or repair the plugin, handle credentials, or
diagnose — if the question is "did the policy load" or "why can't I invoke
this skill", that is leo:doctor, which reads and never writes. Its write
boundary is explicit consent state, the current harness's automatic core MCP
registration/config additions, and an explicitly named automatic connector;
manual providers only print their prerequisites. Hermes projection is applied
at the next session start through `memory.py`; removing Leo's balanced marker
block is the only safely reversible projection action. Config additions and
their one-time backups are not a general rollback mechanism.
