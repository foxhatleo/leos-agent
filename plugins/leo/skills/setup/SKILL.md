---
name: setup
description: >
  Turn on the opt-in wiring a plugin install cannot turn on for itself.
  Every harness installs Leo through its own plugin system, and none of them
  offers an install-time hook, so anything that writes into a file the user
  already owns is asked for once, here, and recorded in machine-local state.
  Idempotent and reversible: running it twice changes nothing the second time.
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
OpenCode no such variable exists — read the absolute path out of the
machine-local state paragraph in the policy already in your context, which had
its placeholders substituted before injection.

With no arguments it reports what is on, what is available, and what each
feature would actually do right now. It changes nothing. Add `--json` for the
same facts as data.

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" enable hermes-memory
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" disable hermes-memory
```

Enabling something already on prints that and exits 0 — re-running is always
safe, and never a reason to check first.

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

It does not install, update, or repair the plugin, and it does not diagnose —
if the question is "did the policy load" or "why can't I invoke this skill",
that is leo:doctor, which reads and never writes. setup only records consent
and flips flags; the projection itself happens at the next session start,
through the same `memory.py` helper every harness already uses.
