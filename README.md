# leos-agent

Leo's portable agent operating policy, version **10.6.0**, installable on Claude
Code, Codex, Cursor, Hermes, Pi, and OpenCode through each harness's own plugin
system.

The policy it carries is short: **the main thread is an orchestrator.**
Investigation, brainstorming, debugging, and mechanical work all run in briefed
subagents, so the main thread never fills up with the files, retries, and logs
that produced an answer — only the answer. A single command you can filter at
the shell stays inline. Work runs at one of two named tiers: **standard**, the
model you are already using, for thinking and judging; **economical**, two
named agent profiles — `leo-runner` for narrow search, reading, testing, and
mechanical work, `leo-executor` for well-specified implementation — shipped as
first-class agent definitions on both Claude Code (`agents/`, Haiku and Sonnet)
and Codex (installed TOML profiles), so the cheaper model is baked into the
agent type rather than chosen per dispatch. Every other harness inherits unless
a machine-local [routing config](#per-machine-model-routing) names models for it.

## What it ships

Beyond the preferences payload: the two economical-tier agent definitions
(`agents/` for Claude Code, `payload/codex-agents/` for Codex), a setup
diagnostic, a routing tuner, a session handoff pair, and three GitHub skills.
The GitHub ones need `gh`, authenticated.

| Skill | What it does | Where |
|---|---|---|
| `review-pr` | Reviews a pull request and stages inline comments as a **pending** review — visible only to you until you submit or discard on GitHub. Never submits. Resolves the originating ticket (Linear, Jira, GitHub issue) from the PR's title, body, or branch when one is named, and adds a spec lens that checks the diff against it. | every skill-loading harness |
| `watch-review` | Arms a watcher that streams direct review requests into the session for `review-pr` to handle, and re-streams one when its head moves. Never surfaces a pull request someone else has approved. Polling is a shell script (`scripts/watch_review.py`), not a model loop: an idle tick is one `gh` call and zero tokens. | **Claude Code only** — built on its Monitor tool |
| `doctor` | Diagnoses this harness's setup, read-only: whether the `<leos-agent>` block is injected and current, what else is loaded into every session (global instruction file, memories, settings, skills), and whether a local checkout passes `scripts/check.py`. Run it with `/doctor`. | every skill-loading harness |
| `tune-routing` | Picks the concrete models behind `leo-runner` and `leo-executor` on this machine, writes them to `~/.leos-agent-local/routing.json`, re-renders the install, and proves the choice with one live dispatch — model strings are never checked against a known-model list, so a typo surfaces at dispatch time and nowhere earlier. Run it with `/tune-routing`. | every skill-loading harness |
| `handoff` | Writes this session's context — goal, what landed, what is next, key files, decisions, gotchas — to a markdown document under `~/.leos-agent-local/handoffs/`, so a later session can pick the work up. Pointers, not contents: it names files rather than pasting them. Run it with `/handoff`. | every skill-loading harness |
| `handon` | Loads a handoff written earlier — in this harness or a different one — and resumes from it, reporting any drift first when the directory, branch, or HEAD has moved since. Loading never consumes a handoff. Run it with `/handon <name>`. | every skill-loading harness |
| `attach-pr` | Attaches the current desktop session to an existing pull request so the app shows its PR card. Creates nothing and pushes nothing. | **Claude Code only** — it drives that app's card |

The Claude-only pair live in `skills-claude/` and `commands-claude/`, listed in
`.claude-plugin/plugin.json` and nowhere else. Hermes receives the preferences
payload but no skills — it has no skill loader.

The watcher records the **head commit** it reviewed each pull request at, under
`~/.leos-agent-local/` (override with `$LEOS_AGENT_LOCAL_PATH`), so a pull
request comes back when someone pushes to it and stays quiet otherwise;
`watch_review.py forget <n>` puts one back in play at its current head. Two
gates keep a continuous watch from being expensive: a pull request another user
has already approved is never surfaced at all, and a new head must hold still
for `--settle` seconds (default 120) before it is emitted, so a burst of pushes
costs one review rather than one per commit.

Handoffs live in the same place, at
`~/.leos-agent-local/handoffs/<name>.md` — a fixed path that needs no plugin
root, so `/handon` reads one with a single `cat` rather than going looking for
it. Nothing there is ever pruned automatically: `handoff.py list [--all]` shows
what exists and `handoff.py rm <name>` is the only way one goes away. The
directory is deliberately outside the plugin, so upgrading or reinstalling can
never take state with it.

## Per-machine model routing

The economical tier only ever had teeth on Claude Code and Codex, because those
are the two harnesses whose model names the payload could hardcode. Everywhere
else, every fan-out ran at the current model — the most expensive shape the
policy has. Which models a harness offers varies by machine and by what an IT
department allows, so the mapping is machine-local config rather than something
the plugin can ship:

```jsonc
// ~/.leos-agent-local/routing.json  (override the directory with $LEOS_AGENT_LOCAL_PATH)
{
  "cursor":   {"runner": "grok-code-fast-1", "executor": "claude-sonnet-4.6"},
  "opencode": {"runner": "anthropic/claude-haiku-4-5"},
  "codex":    {"runner": {"model": "gpt-5.6-luna", "effort": "low"}}
}
```

Keys are harness names; each holds `runner` and/or `executor`, independently —
configuring only `runner` is the common case, since it is the fan-out that
costs. A bare string is shorthand for `{"model": ...}`. Model strings are
free-form and never checked against a known-model list: whatever the harness
accepts goes in verbatim. A misspelled *key*, though, is a hard error, because a
typo that silently left a harness on the expensive model is the one failure this
is here to prevent.

**Nothing reads it at run time.** `leo-install.py` renders the result into the
`<leos-agent>` block it already writes, so a session pays nothing to know its own
routing — no config read, no extra turn. It costs *less* than before: each
machine now carries only its own harness's dispatch line instead of all of them,
which took the installed payload from 4497 bytes to 4315–4344 depending on the
harness. `scripts/measure_context.py` prints the per-harness figure and fails if
an unconfigured harness ever grows past the old one.

Edit the file — by hand, or with `routing.py set --harness <h> --runner
<model>`, which [`/tune-routing`](#what-it-ships) drives end to end — then
re-run the installer to re-render; `leo-install.py <harness> --check` reports
"out of date" until you do, and `/doctor` surfaces it. Installing is
idempotent: same config, same version, same bytes, so a second run reports
`unchanged`.

**The config is yours, never the installer's.** `leo-install.py` only ever reads
it, and never creates, migrates, rewrites, or removes it, including under
`--uninstall`; it lives outside the plugin so an upgrade cannot take it. The one
thing that writes it is `routing.py set` / `unset`, run because you asked: it
creates the file if it is missing, replaces the one role you named, validates
the whole document before writing, and leaves every other harness's entry —
including the bare-string shorthand — exactly as you wrote it. `routing.py show`
prints what is configured; with no file at all, every harness uses its shipped
default and behaviour is exactly what it was before this existed.

Delivery differs by harness only in the last mile: Claude Code gets a `model:`
override alongside `subagent_type:` (the plugin-owned `agents/*.md` are never
rewritten), Codex gets the models substituted into its installed profile TOMLs,
Cursor gets its own `~/.cursor/rules/leos-agent-routing.mdc` because its rules
come straight out of the plugin directory, and the rest get the rendered line in
their global instruction file.

## How it works

The payload lives in exactly one file: [`rules/preferences.md`](rules/preferences.md).

Cursor reads that file natively as an always-apply rule. Every other harness
gets it through its global instruction file, written by
[`scripts/leo-install.py`](scripts/leo-install.py) into a marker block:

```
<leos-agent version="10.6.0">
...the payload...
</leos-agent>
```

Updating replaces that block and nothing else, so anything you wrote in those
files by hand survives an upgrade untouched. The script writes only when the
bytes actually differ, so running it twice is a no-op, and it writes through a
temporary file and an atomic rename, so an interrupted run cannot leave a
half-written instruction file behind.

If it ever finds markers it cannot pair — an opener with no closer, a stray
closer, two blocks — it refuses to touch that file and tells you what to fix.
Guessing there would mean deleting whatever sits between the markers, which is
exactly the content it exists to protect.

| Harness | Global file the installer writes |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` (the `leo-runner` / `leo-executor` agents need no installer step — the plugin's `agents/` directory delivers them) |
| Codex | `~/.codex/AGENTS.md` (plus `~/.codex/agents/leo-runner.toml` and `leo-executor.toml`) |
| Cursor | none — the plugin's always-apply rule delivers it |
| Hermes | `~/.hermes/SOUL.md` (edited only if it already exists) |
| Pi | `~/.pi/agent/AGENTS.md` |
| OpenCode | `~/.config/opencode/AGENTS.md` (plus copied skills and commands) |

**The installer is per-harness and manual.** Running it inside Codex installs Codex and
nothing else; it never writes to another harness's files behind your back, and
it never runs on its own at session start. Install the plugin, then run the
installer once in that harness.

Requires Python 3.9+ and macOS, Linux, or WSL. No symlinks are used anywhere —
installs are real clones and copies.

---

## Claude Code

**Install**

```bash
claude plugin marketplace add foxhatleo/leos-agent
```

```bash
claude plugin install leos-agent@leos-agent --scope user
```

Then, in a Claude Code session, run `/install` (or ask it to use the `install`
skill). That writes the block into `~/.claude/CLAUDE.md`.

**Upgrade**

```bash
claude plugin marketplace update leos-agent
```

```bash
claude plugin install leos-agent@leos-agent --scope user
```

Re-run `/install` afterwards to refresh the block, then start a new session.
Both commands are safe to repeat; installing an already-current version reports
that it is already installed and changes nothing.

**Uninstall**

Run the installer's uninstall first, while the script is still on disk:

```bash
python3 ~/.claude/plugins/cache/leos-agent/leos-agent/10.6.0/scripts/leo-install.py claude --uninstall
```

```bash
claude plugin uninstall leos-agent@leos-agent
```

Optionally drop the marketplace too:

```bash
claude plugin marketplace remove leos-agent
```

---

## Codex

**Install**

```bash
codex plugin marketplace add foxhatleo/leos-agent
```

```bash
codex plugin add leos-agent@leos-agent
```

Then run the `install` skill in a Codex session (`$leos-agent`, then `install`), or
run the script directly:

```bash
python3 ~/.codex/plugins/cache/leos-agent/leos-agent/10.6.0/scripts/leo-install.py codex
```

This writes `~/.codex/AGENTS.md` and installs two economical agents:
`leo-runner` (`gpt-5.6-luna`, low effort) for narrow repeatable work and
`leo-executor` (`gpt-5.6-terra`, medium effort) for well-specified
implementation. Codex plugins cannot ship agent definitions themselves, which
is why the installer writes them.

**Upgrade**

```bash
codex plugin marketplace upgrade leos-agent
```

```bash
codex plugin add leos-agent@leos-agent
```

Re-run the installer, then start a new thread — Codex picks up plugin changes on new
threads only. Re-adding an already-installed plugin is idempotent.

**Uninstall**

```bash
python3 ~/.codex/plugins/cache/leos-agent/leos-agent/10.6.0/scripts/leo-install.py codex --uninstall
```

```bash
codex plugin remove leos-agent@leos-agent
```

```bash
codex plugin marketplace remove leos-agent
```

---

## Cursor

Cursor has no on-disk global rules file — its User Rules live in your synced
Cursor account — so there is nothing for the installer to write. The plugin ships the
payload as an always-apply rule instead, which takes effect as soon as the
plugin is installed.

**Install** — either through the UI, or as a local clone.

In the IDE: open the **Customize** sidebar, add the marketplace
`foxhatleo/leos-agent`, and install **Leo's Agent** at user scope.

Or clone it into Cursor's local plugin directory (a clone, not a symlink):

```bash
git clone https://github.com/foxhatleo/leos-agent ~/.cursor/plugins/local/leos-agent
```

Cursor does not currently expose a reliable non-interactive per-plugin install
command, so those two paths are the supported ones.

**Upgrade**

Refresh the marketplace from the Customize panel, or for a local clone:

```bash
git -C ~/.cursor/plugins/local/leos-agent pull
```

**Uninstall**

Remove the plugin from the Customize panel, or delete the clone:

```bash
rm -rf ~/.cursor/plugins/local/leos-agent
```

There is no block to remove — nothing was written outside the plugin directory.

---

## Hermes

**Install**

If the plugin resolves through the community index:

```bash
hermes plugins install leos-agent
```

Otherwise clone it into the Hermes plugin directory:

```bash
git clone https://github.com/foxhatleo/leos-agent ~/.hermes/plugins/leos-agent
```

Hermes plugins are opt-in, so enable it by adding `leos-agent` to
`plugins.enabled` in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - leos-agent
```

**Run Hermes once before installing.** The payload goes into `~/.hermes/SOUL.md`,
the agent's identity prompt, and Hermes writes its own starter version of that
file on first run. The installer deliberately never creates it — if `SOUL.md` is
missing it reports `skipped` and leaves Hermes' bootstrap alone. Once it exists:

```bash
/leo-install
```

**Upgrade**

```bash
hermes plugins update leos-agent
```

or, for a clone:

```bash
git -C ~/.hermes/plugins/leos-agent pull
```

Then re-run `/leo-install`.

**Uninstall**

```bash
python3 ~/.hermes/plugins/leos-agent/scripts/leo-install.py hermes --uninstall
```

```bash
hermes plugins remove leos-agent
```

Remove the `leos-agent` entry from `plugins.enabled`, and delete the clone if
you made one. Your own `SOUL.md` content is left intact — only the block goes.

**Note on model routing:** Hermes applies a single `delegation.model` to every
child of a `delegate_task` call, so it cannot vary the model per spawn. A
routing config still renders a stanza for it, and the stanza says to inherit and
say so where a per-spawn model is not available.

---

## Pi

**Install**

```bash
pi install git:github.com/foxhatleo/leos-agent
```

Then run the install skill in a pi session:

```
/skill:install
```

Pi pins the git ref it installed and records the package in
`~/.pi/agent/settings.json`; re-running install is idempotent.

**Upgrade**

```bash
pi update git:github.com/foxhatleo/leos-agent
```

Pinned refs are reconciled, never silently advanced — to move to a new tag,
install it explicitly:

```bash
pi install git:github.com/foxhatleo/leos-agent@v10.6.0
```

Re-run `/skill:install` afterwards.

**Uninstall**

```bash
python3 ~/.pi/agent/git/github.com/foxhatleo/leos-agent/scripts/leo-install.py pi --uninstall
```

```bash
pi remove git:github.com/foxhatleo/leos-agent
```

---

## OpenCode

OpenCode loads plugins as npm packages, so this one is published to npm as
`leos-agent`.

**Install**

```bash
opencode plugin leos-agent -g
```

That adds the package to the `plugin` array in `~/.config/opencode/opencode.json`
(or `.jsonc`) and caches it. Bootstrap the installer once by running the script from
the cache — OpenCode's plugin API cannot register skills or commands, so the
first run has to come from the package itself:

```bash
python3 ~/.cache/opencode/packages/leos-agent@latest/node_modules/leos-agent/scripts/leo-install.py opencode
```

That writes `~/.config/opencode/AGENTS.md` and copies the skills and commands into
`~/.config/opencode/skills/` and `~/.config/opencode/commands/`. From then on
`/leo-install` works inside OpenCode. The copies are installed with the plugin
root already resolved to an absolute path — OpenCode sets no resolution env var,
and the copies live apart from the scripts they invoke — so re-run the installer
after clearing or moving the package cache to point them at the new location.

**Upgrade**

```bash
opencode plugin leos-agent -g -f
```

If the cache holds a stale copy, clear it and let OpenCode refetch:

```bash
rm -rf ~/.cache/opencode/packages/leos-agent@*
```

Re-run the bootstrap install command above to refresh the copied files.

**Uninstall**

```bash
python3 ~/.cache/opencode/packages/leos-agent@latest/node_modules/leos-agent/scripts/leo-install.py opencode --uninstall
```

OpenCode has no plugin-remove command, so delete the `"leos-agent"` entry from
the `plugin` array in `~/.config/opencode/opencode.json` **by hand**. The installer
never edits that file: it is JSONC, with your comments in it, and rewriting it
would destroy them. Then clear the cache:

```bash
rm -rf ~/.cache/opencode/packages/leos-agent@*
```

---

## Migrating from v8

Version 10 renames the plugin from `leo` to `leos-agent`, so the old install
does not upgrade in place — remove it first. Most skills that were invoked
as `leo:<name>` are gone; v10 ships a deliberately lean payload plus the three
GitHub skills above, now unprefixed (`review-pr`, not `leo:review-pr`). The
watcher no longer runs under `/loop`: it is a shell process streaming into
Claude Code's Monitor tool, so idle polling costs nothing.

**Claude Code.** The old plugin will show as `failed to load` once the
marketplace points at v10 (`Plugin leo not found in marketplace leos-agent`).
Remove it:

```bash
claude plugin uninstall leo@leos-agent
```

**Codex.** The v8 marketplace entry is pinned to an old commit, and Codex
refuses to re-add a marketplace from a different source. Remove and re-add:

```bash
codex plugin marketplace remove leos-agent && codex plugin marketplace add foxhatleo/leos-agent
```

**OpenCode.** The existing `"leos-agent"` plugin entry stays valid; clear the
cache so it refetches v10.

**Leftover files.** v8 wrote `*.leo-backup` files next to the instruction files
it touched. v10 does not create backups — the block replacement is surgical, and
`--dry-run` shows you any change before it happens. These are safe to delete:

```bash
rm -f ~/.claude/CLAUDE.md.leo-backup ~/.codex/AGENTS.md.leo-backup ~/.config/opencode/AGENTS.md.leo-backup
```

---

## Extending it

The repo root is the plugin. Each harness reads its own manifest from the same
tree, and the three payload directories are shared between them.

**Skills** live in `skills/<name>/SKILL.md`, or `skills-claude/<name>/SKILL.md`
for one only Claude Code can use; commands mirror that with `commands/` and
`commands-claude/`. The two `-claude` directories are listed in
`.claude-plugin/plugin.json` and nowhere else. Keep the frontmatter of a
portable skill to `name` and `description`, plus
`disable-model-invocation: true` on a skill that must never fire on its own.
Codex uses the matching sibling `agents/openai.yaml` with
`policy.allow_implicit_invocation: false`; harnesses without a control for it
get the constraint stated in the description. That subset is what all five skill-loading harnesses accept, and
anything richer will parse on Claude Code and be ignored or rejected elsewhere.
Claude Code, Codex, Cursor, and Pi load `skills/` straight from their manifests;
OpenCode gets a copy from the installer.

**Commands** live in `commands/<name>.md`. Claude Code and Cursor read the
directory from their manifests; OpenCode gets a copy. Codex dropped custom
prompts in favour of skills, so add a skill there instead.

### Two conventions, both enforced by `scripts/check.py`

**Progressive disclosure.** `SKILL.md` is the *dispatch contract* — what the
main thread does. The procedure a subagent follows goes in
`skills/<name>/reference/*.md`, which the brief points at by path. `review-pr`
is the worked example: the main thread loads a 3.2 KB contract, the reviewer
subagent reads `reference/procedure.md`, and the lens sub-subagents read
`reference/lenses.md` that the reviewer itself never loads. Before the split the
main thread and the reviewer each loaded the same 21 KB file, and every turn
after that re-read it. `tune-routing` does the same with its per-harness model
discovery, in `reference/harnesses.md`, which only a run that actually tunes
ever loads. Split a file out only when some run genuinely does not read it;
moving prose around costs the same tokens.

**Invocation split.** A skill is either *user-invoked* — reached by typing its
slash command, and carrying `disable-model-invocation: true` — or *deliberately
model-invocable*, reached when the model decides the task fits. On Claude Code
the flag also drops the skill's description from the always-loaded skill
listing, which is the larger saving: a description is context in every session,
invoked or not. Only `review-pr` and `handon` are model-invocable here, because
they are the two you would phrase in words ("review PR 41", "pick up where I
left off") rather than by name; `check.py` fails the build on any other skill
that omits the flag. A user-invoked skill may invoke a model-invoked one, but
never chains another user-invoked skill.

A description says **when to reach for this** and **what it is not** — never how
the skill works. The mechanism is what the body is for, and every word of it in
the description is paid for in sessions that never invoke the skill.

**Hooks** live in `hooks/`, wired but empty — v10 enforces its policy through
the payload rather than by intercepting tool calls. There are two files because
the formats genuinely differ: `hooks.json` (PascalCase events) serves Claude
Code and Codex, which both auto-load it and must never name it in their
manifests, and `hooks-cursor.json` (camelCase, `version: 1`) serves Cursor,
which does name it. See [`hooks/README.md`](hooks/README.md) for how to add one,
including the Hermes, OpenCode, and Pi equivalents, which are code rather than
JSON.

## Development

Run the checks:

```bash
python3 scripts/check.py
```

The structural check asserts that the version matches across every manifest,
the marketplace entry, and this README; that each manifest carries what its
harness requires and every declared path exists; that both hook files parse in
their own format; and that injection is idempotent, uninstall round-trips, and
malformed markers are refused.

Run the behavioral tests:

```bash
python3 -m unittest discover -s tests -v
```

Measure the repository-controlled static prompt footprint and enforce its
committed ceilings:

```bash
python3 scripts/measure_context.py --check
```

The measurement is a byte-based proxy for always-listed or dispatch-loaded
text. It deliberately does not claim to measure total task tokens or credits,
which also depend on conversation history, cache state, tool output, and the
number and model of spawned agents.

Preview any install without writing:

```bash
python3 scripts/leo-install.py <harness> --dry-run
```

**Both Claude Code and Codex cache a plugin by version**, so reinstalling while
the version is unchanged is a no-op and quietly leaves the old code in place —
you will be testing the previous build without being told. While iterating,
either uninstall and reinstall:

```bash
claude plugin uninstall leos-agent@leos-agent && claude plugin install leos-agent@leos-agent --scope user
```

or replace the cachebuster suffix in the Codex manifest with one in the form
`10.6.0+codex.local-YYYYMMDD-HHMMSS` and re-add. Either way, plugin changes only
reach a **new** session or thread.

`--check` exits non-zero when a file is out of date, and `--force` replaces a
copied file that something else has since overwritten.

To release: bump the version in `package.json`, the three `plugin.json` files,
`.claude-plugin/marketplace.json`, `plugin.yaml`, and every mention in this
README (the uninstall commands embed it in cache paths — `check.py` fails on any
stale one); run `scripts/check.py`; then push a `v`-prefixed tag.

Pushing that tag is the whole release. `.github/workflows/release.yml` runs the
tests and both checks, refuses a tag that disagrees with `package.json`,
inspects the tree npm would ship, and publishes to npm for OpenCode. It
authenticates by OIDC trusted publishing, so there is no token in the repository
— npm's configuration names this workflow by path, and renaming the file breaks
publishing until npm is updated to match. Publishing is idempotent: a version
already on the registry is a no-op, and a lookup that fails for any reason other
than a confirmed 404 aborts rather than assuming the version is absent.

Check what a publish would contain, without publishing:

```bash
python3 scripts/publish-npm.py --dry-run
```

MIT licensed.
