# Per-harness model discovery and last mile

Read this only when tuning. It answers two questions for the harness you are in:
**what models can this machine actually dispatch**, and **where does the choice
land once written**.

**Discovery lists what an account can see, not what this machine can dispatch.**
A model can appear in a picker and still be refused at spawn — a different
gateway, an IT allowlist, a stale config. That gap is the whole reason
`/tune-routing` ends in a live probe; nothing in this file substitutes for it.

Every entry below is ordered **try this, then this, then ask Leo**. Where a
command is not listed for a harness, it is because there is no verified one —
ask rather than guessing at a flag.

## Discovery

| Harness | Where the real list is |
|---|---|
| `claude` | The in-session `/model` picker is authoritative. Both aliases (`haiku`, `sonnet`, `opus`) and dated IDs (`claude-haiku-4-5`) are accepted. `~/.claude/settings.json` shows the current default. |
| `codex` | The in-session `/model` picker, then `~/.codex/config.toml` (`model`, and any `[model_providers]` entries). Effort is a real second dimension here: `minimal`, `low`, `medium`, `high`. |
| `cursor` | No CLI enumeration exists. The composer's model dropdown, or Settings → Models, is the only list — **ask Leo to read it out.** Names look like `grok-code-fast-1` or `claude-haiku-4-5`. |
| `hermes` | `~/.hermes/config.yaml`, key `delegation.model`. See the warning below before writing anything. |
| `pi` | `~/.pi/agent/settings.json` for what pi is configured with, then whatever the session itself exposes. If neither is conclusive, ask. |
| `opencode` | `~/.config/opencode/opencode.json` for the configured providers, then the session's own model list. Names are provider-qualified: `anthropic/claude-haiku-4-5`. |

**The shipped defaults you are trying to beat.** Only set a value that is
cheaper than these; on every other harness the baseline is the current model.

| Harness | runner | executor |
|---|---|---|
| `claude` | `haiku` (`agents/leo-runner.md`) | `sonnet` (`agents/leo-executor.md`) |
| `codex` | `gpt-5.6-luna`, effort `low` | `gpt-5.6-terra`, effort `medium` |
| everything else | inherits | inherits |

## Last mile

Where the choice actually lands, and what it takes to pick it up.

Most of this needs no installer run at all any more. The payload is read live
from the plugin at session start, and it renders the routing stanza as it is
read — so for three of the six harnesses, writing the config IS the last mile
and a fresh session is the only step left. Codex, Cursor and OpenCode still
need `leo-install.py <harness>`, because their per-machine half lands in a
file rather than in the payload.

| Harness | Where the choice goes | To pick it up |
|---|---|---|
| `claude` | Rendered into the payload the `SessionStart` hook emits. The plugin's `agents/*.md` are **never** rewritten. | Start a new session. |
| `codex` | Substituted into `~/.codex/agents/leo-runner.toml` and `leo-executor.toml` — **run `leo-install.py codex`**. An unset effort keeps the profile's shipped one. | Start a new thread — Codex picks up agent changes on new threads only. |
| `cursor` | `~/.cursor/rules/leos-agent-routing.mdc`, its own always-apply rule — **run `leo-install.py cursor`**. Cursor reads the payload straight out of the plugin, so this file is the only per-machine half. | Reload the window. |
| `hermes` | Rendered into the system-prompt section `register(ctx)` installs. | Start a new session. |
| `pi` | Rendered into the payload the extension appends to the system prompt. | Start a new session. |
| `opencode` | `~/.config/opencode/leos-agent-routing.md`, its own file in `instructions` — **run `leo-install.py opencode`**. `instructions` reads `preferences.md` un-rendered, so the payload's own routing region never varies. | Start a new session. |

## Harnesses that cannot vary the model per spawn

**Hermes** applies a single `delegation.model` to every child of a
`delegate_task` call, so it cannot route `leo-runner` and `leo-executor`
differently. Writing a config for it is still legitimate — the stanza renders,
and it says to inherit and say so where a per-spawn model is not available — but
tell Leo that before writing, and **skip the live probe**: there is nothing
per-spawn to verify.

**Pi** and **OpenCode** are not known to have this limitation, but confirm in
the session that you can actually pass a model on a spawn before probing. If you
cannot, treat the harness as Hermes: write if Leo wants it, report it
unverifiable, and do not claim the tier is active.
