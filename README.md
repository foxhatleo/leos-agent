# leos-agent

Leo's portable agent operating policy, version **11.202609070.0**, installable on Claude
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
| `review-usage` | Reads many sessions across every harness on this machine — a time window, not one session — and reports where the tokens went and how well the policy actually held: routing compliance, guard blocks and whether they were re-dispatched, over- and under-delegation, cache health. The scan is a script, not a prompt, so it costs a second rather than a model's worth of transcript reading. Run it with `/review-usage`. | every skill-loading harness |
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

**Read once per session now, not never.** Most harnesses render the routing
stanza live: `emit_payload.py` calls `routing.load()` every time it runs — on
a `SessionStart` hook (Claude Code, Codex), inside Hermes's frozen
session-scoped prompt section, and on every *turn* for Pi, since
`before_agent_start` has no cheaper hook to sit on (see `pi-extension.js`).
Codex's and Cursor's per-machine halves are still baked at install time —
into the profile TOMLs and the `.mdc` rule respectively — because neither is
otherwise re-rendered. Either way this costs one read of a small local JSON
file, never a network call or a re-render of the whole payload. The bytes it
adds are unchanged from before this delivery mechanism moved: each machine
still carries only its own harness's dispatch line rather than all of them,
which is what keeps the rendered payload at 4222–4251 bytes depending on the
harness (measured with no routing configured; a configured harness costs a
little more, bounded by the model names chosen) against the pre-split figure
of 4497. `scripts/measure_context.py --check` enforces the ceiling and prints
the per-harness figure.

Edit the file — by hand, or with `routing.py set --harness <h> --runner
<model>`, which [`/tune-routing`](#what-it-ships) drives end to end — and it
takes effect at the next session with nothing else to run on Claude Code,
Hermes, and Pi, all of which read it live. Codex, Cursor and OpenCode still
need the installer re-run afterwards, because their halves are baked into
files at install time; `leo-install.py <harness> --check` reports "out of
date" for those three until you do, and `/doctor` surfaces it. Installing is
idempotent: same config, same version, same bytes, so a second run reports
`unchanged`.

OpenCode needs a second file for a reason worth knowing: its `instructions`
list reads `rules/preferences.md` straight off disk, **un-rendered**, so the
routing region in that file keeps its shipped default no matter what is
configured. The per-machine half therefore ships as its own
`~/.config/opencode/leos-agent-routing.md`, written only when routing is
actually configured for OpenCode, and added to `instructions` alongside the
payload — exactly the shape Cursor has had all along, and for exactly the same
reason.

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
rewritten), rendered live at session start. Codex gets the models substituted
into its installed profile TOMLs by the installer, because Codex plugins
cannot ship agent definitions and nothing re-renders them between installer
runs. Cursor gets its own `~/.cursor/rules/leos-agent-routing.mdc`, also
written by the installer, because its rules come straight out of the plugin
directory and are not otherwise re-rendered per session. Hermes and Pi get
the rendered dispatch line inside the payload itself, live, at whatever
cadence that harness re-renders it (see the delivery table under
[How it works](#how-it-works)). OpenCode gets nothing — see above.

## The dispatch guard

The payload has always said that a subagent dispatch must name a model. Prose
alone did not hold: a forgotten dispatch inherits the parent's expensive model
and pays a cold cache write per child, which is the single most expensive shape
this policy has. From 11.202609070.0 that half of the rule is enforced by a hook instead,
and the prose it replaced came out of the always-loaded payload — enforcement in
code costs **zero** context per turn, so the guard paid for itself in bytes
before saving a cent.

`scripts/dispatch_guard.py` runs before a subagent dispatch and refuses exactly
one thing: an agent selected with a brief, **no model named**, on a harness that
can name one. Three ways to comply, all of them one word:

| Instead of | Use | For |
|---|---|---|
| a generic agent, no model | `subagent_type: "leo-runner"` | reading, search, tests, logs, codemods, fan-out |
| a generic agent, no model | `subagent_type: "leo-executor"` | an approved plan, a well-specified change |
| a generic agent, no model | `model: "<name>"` | investigation and debugging — naming it *is* the stated reason |

A plugin install namespaces the type — dispatch `leos-agent:leo-runner` rather
than `leo-runner`; the guard accepts either.

It never picks a model for you. It cannot force cheap work onto an expensive
problem, so it cannot cause a quality regression — only an explicit choice. It
is also deliberately narrow: a false block costs one re-dispatch, while a caught
inherited fan-out saves the cold prefix of every child, so the margin only holds
while the rule refuses to make judgment calls.

Detection is by **argument shape**, not tool name: a dispatch is a call that
selects an agent and carries a brief. A shape the guard does not recognise is
left alone, so a harness it has never met keeps working. MCP tools are never
guarded. Anything that goes wrong inside the guard allows the call and records
`decision: "error"`, kept distinct from a decision to allow, because a guard
that dies quietly is worse than no guard.

```
LEOS_AGENT_DISPATCH_GUARD=on       block (default)
                          warn     record, never block
                          off      disabled entirely
                          verbose  also put the over-delegation notice in front of the model
LEOS_AGENT_DISPATCH_LOG_PROMPTS=1  debug only: keep 200 chars of brief text in the log
```

**Per harness.** Claude Code dispatches `Agent` with `subagent_type`, and a
plugin install namespaces it — both forms are tiers. Codex dispatches
`spawn_agent`, which selects behaviour by `model` and `reasoning_effort` rather
than by naming an agent, so there the guard requires `model`; its `message` is
encrypted, so the over-delegation heuristic and block-conversion tracking do not
apply on Codex. Hermes and OpenCode run the same policy in-process, blocking by
directive and by throw respectively. Pi has no hook surface.

**Codex hash-pins hooks.** A new version of the guard enforces nothing there
until it is re-approved through `/hooks`, and the symptom is silence — zero
Codex rows in `dispatch_log.py report`.

### What it records

`~/.leos-agent-local/dispatch.jsonl`, one line per dispatch, mode `0600`,
rotated at 1 MiB with one generation kept — bounded at 2 MiB forever.

It stores **no prompt text and no paths.** Prompts, sessions and working
directories are truncated SHA-256. The prompt hash is what makes the report
meaningful: a blocked brief whose hash comes back naming a tier is a block that
worked, and one that never returns was abandoned work rather than a saving.
Delete it whenever you like — nothing depends on its history.

```bash
python3 scripts/dispatch_log.py report
python3 scripts/usage_scan.py --since 7d
```

## How it works

The payload lives in exactly one file: [`rules/preferences.md`](rules/preferences.md).
Every harness now reads it live, out of the plugin directory, at or near session
start — upgrading the plugin *is* upgrading the policy, with no render-to-disk
step in between.

Cursor reads that file directly as an always-apply rule; it was the model for
everything that follows. Every other harness runs
[`scripts/emit_payload.py`](scripts/emit_payload.py), which strips the
frontmatter, renders this machine's [routing](#per-machine-model-routing)
stanza, and prints the result — the trigger and the plumbing differ by harness:

| Harness | How the payload arrives | What the installer still does |
|---|---|---|
| Claude Code | a `SessionStart` hook (`hooks/hooks.json`, auto-discovered) runs `emit_payload.py`; its stdout becomes session context | nothing for the payload — `agents/` ships `leo-runner`/`leo-executor` directly; only cleans up a `<leos-agent>` block a pre-11.0 install left in `~/.claude/CLAUDE.md` |
| Codex | the same `hooks/hooks.json` entry — Codex auto-discovers it too, aliasing `${CLAUDE_PLUGIN_ROOT}` to its own plugin root | writes `~/.codex/agents/leo-runner.toml` and `leo-executor.toml`, since Codex plugins cannot ship agent definitions; cleans up a legacy `~/.codex/AGENTS.md` block |
| Cursor | native — `rules/preferences.md` read straight off the plugin directory as an always-apply rule | writes `~/.cursor/rules/leos-agent-routing.mdc`, only when routing is configured for Cursor |
| Hermes | `register(ctx)` in `__init__.py` calls `ctx.register_system_prompt_section("leos-agent", ..., position="after_memory")` — rendered once per session and frozen, so it never invalidates the cache mid-session | cleans up a legacy `~/.hermes/SOUL.md` block; `SOUL.md` itself is never written any more |
| Pi | `pi-extension.js` appends `emit_payload.py`'s output to the system prompt on `before_agent_start`, which fires every *turn*, not once per session — the one harness that pays a spawn per turn rather than per session — and contributes `skills/` via `resources_discover` | cleans up a legacy `~/.pi/agent/AGENTS.md` block |
| OpenCode | a one-time `"instructions": ["<plugin-root>/rules/preferences.md"]` line in `~/.config/opencode/opencode.json`, read at startup | copies skills and commands into `~/.config/opencode/skills/` and `commands/` (OpenCode's JS-only plugin API cannot register those); prints the `instructions` line and reports it outstanding until it's present — the file is JSONC with your comments in it, so the installer never edits it itself; cleans up a legacy `~/.config/opencode/AGENTS.md` block |

**The `<leos-agent version="...">` marker block still exists**, but only for
migration and uninstall now — nothing writes it on a fresh install. Malformed
markers are still refused rather than guessed at: an opener with no closer, a
stray closer, two blocks all stop the run and tell you what to fix, because
guessing would mean deleting whatever sits between them. Where a pre-11.0
install left a block behind, a normal run strips it and reports `migrated`,
preserving whatever surrounding text you wrote by hand; `--uninstall` does the
same cleanup, so either command clears the leftover.

Determinism is part of the contract, not an implementation detail: two runs of
`emit_payload.py` must produce byte-identical output, or the session's cached
prompt prefix stops being cacheable and every session pays a full cold write —
so nothing in the render path may read the clock, an absolute path, or git
state. It also fails open, silently, on stdout: any error prints nothing, exits
0, and leaves a breadcrumb in `$LEOS_AGENT_LOCAL_PATH/emit-payload.log` instead
of injecting a traceback into the session as context.

Requires Python 3.9+ and macOS, Linux, or WSL. No symlinks are used anywhere —
installs are real clones and copies.

## Multi-surface and cloud reach

"One install per harness" undersells how many surfaces a single install
reaches — and where it doesn't:

- **Claude Code's CLI, VS Code extension, JetBrains plugin, and desktop local
  sessions share `~/.claude`.** The VS Code docs describe plugin management as
  using "the same CLI commands under the hood," and the JetBrains plugin runs
  the `claude` CLI rather than bundling its own — one `claude plugin install`
  reaches all four.
- **Claude Code cloud/web sessions do not** — `~/.claude/CLAUDE.md` is
  documented as not carried into them, so the old block-injection design never
  reached the web at all. The plugin route does: declare `leos-agent` under
  `enabledPlugins` in the repo's `.claude/settings.json` and it installs at
  session start. That is a real gain of this design — the policy reaches cloud
  for the first time.
- **Desktop WSL sessions have no plugin support**; SSH sessions read the
  *remote* host's `~/.claude`, so the plugin needs installing on each SSH
  target separately — a local install does not follow you there.
- **The desktop Cowork tab is a separate, account-synced config surface** and
  will not see a CLI install.
- **Codex's IDE extension does not support plugins at all**, though it does
  read `~/.codex/AGENTS.md` and the installed agent TOMLs — so a migrated
  instruction file and the agent profiles still reach it; the live hook
  delivery does not.
- **Cursor Cloud Agents do not see plugin-shipped rules.** User Rules are
  account-synced and do reach them; a plugin's rules live on local disk and
  stop there.
- **Hermes profiles and OpenCode's `OPENCODE_CONFIG_DIR` each create a second,
  invisible config scope** — a plugin installed under one is invisible under
  the other.

Upgrades need a new session almost everywhere: Claude Code's
`/reload-plugins` or a VS Code restart banner, Codex "start a new session,"
Cursor's Reload Window, Hermes a restart or `/restart`, OpenCode only rereads
its config at startup.

---

## Claude Code

**Install**

```bash
claude plugin marketplace add foxhatleo/leos-agent
```

```bash
claude plugin install leos-agent@leos-agent --scope user
```

Nothing else to install. `hooks/hooks.json`'s `SessionStart` hook is
auto-discovered, and the payload starts arriving at the next session — no
`/install` run needed. If this checkout has a `<leos-agent>` block in
`~/.claude/CLAUDE.md` left by a version before 11.0, see Upgrade below to clear
it.

**Upgrade**

Claude Code auto-updates an installed plugin in the background roughly once
per session, so most of the time this is zero commands — start a new session
(`/reload-plugins`, or restart in VS Code) and the new payload is already
live. To force it immediately:

```bash
claude plugin marketplace update leos-agent
```

```bash
claude plugin install leos-agent@leos-agent --scope user
```

Both commands are safe to repeat; installing an already-current version
reports that it is already installed and changes nothing. If you're
upgrading a checkout that still carries a `<leos-agent>` block from before
this delivery mechanism, run the installer once to strip it — it reports
`migrated` and leaves the rest of `~/.claude/CLAUDE.md` exactly as you wrote
it:

```bash
python3 ~/.claude/plugins/cache/leos-agent/leos-agent/11.202609070.0/scripts/leo-install.py claude
```

**Uninstall**

Run the installer's uninstall first, while the script is still on disk — it
only has a legacy block to clean up, but do it before the plugin cache is gone:

```bash
python3 ~/.claude/plugins/cache/leos-agent/leos-agent/11.202609070.0/scripts/leo-install.py claude --uninstall
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

The payload arrives the same way it does on Claude Code: `hooks/hooks.json`'s
`SessionStart` hook is auto-discovered, and Codex aliases
`${CLAUDE_PLUGIN_ROOT}` to its own plugin root, so no Codex-specific hook file
is needed. The installer is still required for what Codex plugins cannot ship
on their own — the two economical agent profiles:

```bash
python3 ~/.codex/plugins/cache/leos-agent/leos-agent/11.202609070.0/scripts/leo-install.py codex
```

This writes `~/.codex/agents/leo-runner.toml` (`gpt-5.6-luna`, low effort) and
`leo-executor.toml` (`gpt-5.6-terra`, medium effort), and cleans up a
`<leos-agent>` block a pre-11.0 install left in `~/.codex/AGENTS.md`. Codex
trusts a hook by the hash of its command, so the command string in
`hooks/hooks.json` is deliberately constant — a payload edit alone never
requires re-approving the hook through `/hooks`.

**Upgrade**

```bash
codex plugin marketplace upgrade leos-agent
```

```bash
codex plugin add leos-agent@leos-agent
```

Re-run the installer to refresh the agent TOMLs (a no-op unless your routing
config changed), then start a new thread — Codex picks up plugin changes on
new threads only. Re-adding an already-installed plugin is idempotent, and the
hook needs no re-approval since its command string never changes.

**Uninstall**

```bash
python3 ~/.codex/plugins/cache/leos-agent/leos-agent/11.202609070.0/scripts/leo-install.py codex --uninstall
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
Cursor account — so there was never anything for the installer to write here;
this is the harness the rest of leos-agent's live delivery was modeled on. The
plugin ships the payload as an always-apply rule instead, which takes effect
as soon as the plugin is installed. The installer still has one job for
Cursor: writing `~/.cursor/rules/leos-agent-routing.mdc`, and only when
[routing](#per-machine-model-routing) is actually configured for it.

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

Nothing else to install. `register(ctx)` in `__init__.py` calls
`ctx.register_system_prompt_section("leos-agent", ..., position="after_memory")`
— the cache-safe path: it renders once per session and freezes, rather than
re-rendering on every turn. `~/.hermes/SOUL.md` is not written by this plugin
at all, so there is no "run Hermes once first" step any more. On a Hermes build
old enough to lack `register_system_prompt_section`, the plugin degrades
silently and delivers nothing — upgrade Hermes.

`/leo-install` still exists, mainly for `--dry-run` and `--uninstall`; a plain
run is a no-op unless a pre-11.0 install left a `<leos-agent>` block in
`SOUL.md`, in which case it strips it and reports `migrated`.

**Upgrade**

```bash
hermes plugins update leos-agent
```

or, for a clone:

```bash
git -C ~/.hermes/plugins/leos-agent pull
```

Restart Hermes (or `/restart`) for a fresh session to pick up the new payload.
If this checkout still carries a legacy `<leos-agent>` block in `SOUL.md`, run
`/leo-install` once to strip it.

**Uninstall**

```bash
python3 ~/.hermes/plugins/leos-agent/scripts/leo-install.py hermes --uninstall
```

```bash
hermes plugins remove leos-agent
```

Remove the `leos-agent` entry from `plugins.enabled`, and delete the clone if
you made one. Your own `SOUL.md` content is left intact — only a legacy block,
if one is present, goes.

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

Nothing else to install. `pi-extension.js` registers `before_agent_start`,
which runs `scripts/emit_payload.py` and appends its output to the system
prompt, and `resources_discover`, which contributes `skills/` directly — no
`/skill:install`, no `~/.pi/agent/AGENTS.md` write. Unlike Claude Code's and
Codex's session-scoped hook, `before_agent_start` fires on every turn, not
once per session, so Pi pays one `python3` spawn per turn rather than per
session.

Pi pins the git ref it installed and records the package in
`~/.pi/agent/settings.json`; re-running install is idempotent. If a legacy
`<leos-agent>` block exists in `~/.pi/agent/AGENTS.md` from a pre-11.0 install,
strip it with the installer:

```bash
python3 ~/.pi/agent/git/github.com/foxhatleo/leos-agent/scripts/leo-install.py pi
```

**Upgrade**

```bash
pi update git:github.com/foxhatleo/leos-agent
```

Pinned refs are reconciled, never silently advanced — to move to a new tag,
install it explicitly:

```bash
pi install git:github.com/foxhatleo/leos-agent@v11.202609070.0
```

Start a new session afterwards; there is nothing else to re-run.

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
the cache — OpenCode's JS-only plugin API cannot register skills, commands, or a
payload source, so the first run has to come from the package itself:

```bash
python3 ~/.cache/opencode/packages/leos-agent@latest/node_modules/leos-agent/scripts/leo-install.py opencode
```

That copies the skills and commands into `~/.config/opencode/skills/` and
`~/.config/opencode/commands/`, cleans up a `<leos-agent>` block a pre-11.0
install left in `~/.config/opencode/AGENTS.md`, and — since the payload itself
now arrives through a one-time line in `opencode.json` rather than a written
file — prints the exact line to add and reports it outstanding until it's
there:

```jsonc
"instructions": ["<plugin-root>/rules/preferences.md"]
```

The installer never adds that line for you: `opencode.json` is JSONC, with
your comments in it, and rewriting it would destroy them. Add it by hand,
once — OpenCode reads it at startup from then on. From then on `/leo-install`
works inside OpenCode for re-copying the skills and commands. Those copies are
installed with the plugin root already resolved to an absolute path —
OpenCode sets no resolution env var, and the copies live apart from the
scripts they invoke — so re-run the installer after clearing or moving the
package cache to point them at the new location.

**Upgrade**

```bash
opencode plugin leos-agent -g -f
```

If the cache holds a stale copy, clear it and let OpenCode refetch:

```bash
rm -rf ~/.cache/opencode/packages/leos-agent@*
```

Re-run the bootstrap install command above to refresh the copied skills and
commands — the `instructions` line does not need touching, since it just
points at the plugin root and the payload behind it updates live. OpenCode
only rereads its config at startup, so restart it to pick up either change.

**Uninstall**

```bash
python3 ~/.cache/opencode/packages/leos-agent@latest/node_modules/leos-agent/scripts/leo-install.py opencode --uninstall
```

OpenCode has no plugin-remove command, so **by hand**: delete the
`"leos-agent"` entry from the `plugin` array, and remove the `"instructions"`
line pointing at this plugin's `rules/preferences.md`, in
`~/.config/opencode/opencode.json`. The installer edits neither — same JSONC
reason as above. Then clear the cache:

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

**One-time setup:** `git config core.hooksPath .githooks` activates
`.githooks/pre-commit`, which stamps today's version with `scripts/bump.py`
and runs `scripts/check.py` on every commit — so a normal commit already
carries a canonical version and a green structural check, and you should not
need to run either by hand for a routine change. The scheme is
`11.YYYYMMDDX.0`: major pinned at 11, minor the UTC calendar date with a
same-day serial digit appended, patch always 0.

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
`11.202609070.0+codex.local-YYYYMMDD-HHMMSS` and re-add. Either way, plugin changes only
reach a **new** session or thread.

`--check` exits non-zero when a file is out of date, and `--force` replaces a
copied file that something else has since overwritten.

To release: the pre-commit hook has already stamped the version via
`scripts/bump.py` on your latest commit if `core.hooksPath` is set up as
above. Otherwise run it by hand:

```bash
python3 scripts/bump.py
```

That rewrites every `major.minor.patch` string it owns — `package.json`, the
three `plugin.json` files, `.claude-plugin/marketplace.json`, `plugin.yaml`,
and every mention in this README, including the uninstall commands' cache
paths — in one pass; `scripts/check.py` still fails the build on any stale
one it finds. Then push a `v`-prefixed tag matching that version.

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
