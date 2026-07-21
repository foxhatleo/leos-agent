# Leo

Leo is a portable agent operating policy for Claude Code, Codex, Cursor, and Hermes. It packages cost-tiered model routing, seven specialist roles, process skills, execute-then-review discipline, and a narrow catastrophic-command guard as native plugins.

The repository does not need to be cloned for normal use. Each harness installs Leo through its own plugin system, and updates come through that system.

## Install

### Claude Code

```sh
claude plugin marketplace add foxhatleo/leos-agent
claude plugin install leo@leos-agent
```

Update or remove it with:

```sh
claude plugin marketplace update leos-agent
claude plugin update leo@leos-agent
claude plugin uninstall leo@leos-agent
```

Start a new session after installing or updating. Claude loads Leo's native agents, skills, settings, and hooks from the cached plugin.

Verify the installed version and component inventory with `claude plugin list` and `claude plugin details leo@leos-agent`.

### Codex

```sh
codex plugin marketplace add foxhatleo/leos-agent
codex plugin add leo@leos-agent
```

Update or remove it with:

```sh
codex plugin marketplace upgrade leos-agent
codex plugin add leo@leos-agent
codex plugin remove leo@leos-agent
```

Start a new task after installing or updating. Codex loads the skills and hooks from the plugin; Leo dispatches generic subagents with an explicit role prompt, model, and reasoning effort instead of installing global agent TOMLs.

Verify that `leo@leos-agent` is installed with `codex plugin list`.

### Cursor

Once Leo is listed in Cursor's public marketplace:

```text
/add-plugin leo
```

Before marketplace approval, install it directly from this public repository:

```text
/add-plugin leo@https://github.com/foxhatleo/leos-agent
```

Use Cursor's Customize → Plugins screen to verify, update, disable, or remove it. Cursor agents inherit the model selected in the UI; Leo recommends a tier but does not claim to enforce an arbitrary model name per subagent.

### Hermes

OpenRouter authentication must already be configured, then run:

```sh
hermes plugins install foxhatleo/leos-agent --enable
```

Update, disable, or remove it with:

```sh
hermes plugins update leo
hermes plugins disable leo
hermes plugins remove leo
```

Hermes installs the Git repository into its plugin directory and loads the root `plugin.yaml` and `__init__.py` entrypoint. Leo registers its skills as `leo:<skill>` and injects the routing policy before model calls.

Verify the enabled state with `hermes plugins list`; inside a running session, `/plugins` shows the loaded plugin.

## Model tiers

Tier names describe work, not a universal provider model. The canonical defaults live in [`plugins/leo/config/models.json`](plugins/leo/config/models.json).

| Tier | Typical work | Claude Code | Cursor | Codex | Hermes via OpenRouter |
|---|---|---|---|---|---|
| Fable | Expert arbitration | `fable` | GPT-5.6 Sol | `gpt-5.6-sol`, max | `moonshotai/kimi-k3` |
| Opus | Planning, investigation, review | `opus[1m]` | Grok 4.5 | `gpt-5.6-sol`, high | `moonshotai/kimi-k3` |
| Sonnet | Implementation | `sonnet[1m]` | Grok 4.5 | `gpt-5.6-terra`, medium | `z-ai/glm-5.2` |
| Haiku | Exploration and mechanical work | `haiku` | Composer 2.5 | `gpt-5.6-luna`, low | `z-ai/glm-5.2` |

The role mapping is expert → Fable; planner, investigator, and reviewer → Opus; implementer → Sonnet; executor and explore → Haiku.

### Change model defaults

- Maintainers edit only `plugins/leo/config/models.json`, then run `python3 plugins/leo/scripts/render_adapters.py`. CI runs the same command with `--check` to reject generated-file drift.
- Claude users set `fable_model`, `opus_model`, `sonnet_model`, and `haiku_model` when installing with repeated `--config key=value` flags. To change them later, update `pluginConfigs["leo@leos-agent"].options` in `~/.claude/settings.json`, then run `/reload-plugins`. These non-sensitive values are substituted into native agent definitions.
- Codex users can override a model for one request in the prompt, or persist a Leo tier override in native `AGENTS.md`. Explicit user instructions take precedence over bundled defaults.
- Cursor users select the mapped model in the native model picker before starting a homogeneous tier batch. Generated Cursor agents use `model: inherit`.
- Hermes users switch the parent with `/model` and configure one delegation model for all native children. A delegation batch cannot mix Kimi and GLM.

For a Fable/Opus Hermes batch:

```text
/model moonshotai/kimi-k3 --provider openrouter
```

```yaml
delegation:
  provider: openrouter
  model: moonshotai/kimi-k3
```

For a Sonnet/Haiku Hermes batch:

```text
/model z-ai/glm-5.2 --provider openrouter
```

```yaml
delegation:
  provider: openrouter
  model: z-ai/glm-5.2
```

Group delegated work into homogeneous Kimi or GLM batches and change this native Hermes setting between batches.

## What the plugin provides

- `using-leo`: the session policy for model routing, delegation, and execute-then-review.
- Seven roles: expert, planner, investigator, reviewer, implementer, executor, and explore.
- Process skills: `brainstorming`, `writing-plans`, `executing-plans`, `debugging`, `test-first`, `verification`, `delegation`, `worktrees`, and `finishing-a-branch`.
- Operational skills: `resolve-ticket`, `review-pr`, and `watch-review`.
- Session bootstrap hooks for Claude Code, Codex, and Cursor, plus native policy injection for Hermes.
- A shared bash guard that blocks a narrow class of accidental home/system-scale destructive commands.

The bash guard is an accident-prevention tripwire, not an adversarial shell sandbox. It deliberately does not try to enumerate every obfuscation or malicious-command technique; each harness's permissions and sandbox remain the security boundary.

## MCP integrations

Leo does not bundle MCP servers. Install and authenticate Linear, Slack, Atlassian, Google, Vercel, Notion, or other MCP integrations independently through the harness that will use them. This keeps the workflow policy separate from personal services, credentials, and organization-specific access.

## Machine-local state

Skills that persist state write JSON under:

```text
${LEOS_AGENT_PATH:-~/.leos-agent}/local/<skill-or-agent-name>.json
```

`~/.leos-agent` is now a data location, not an installation clone. State is separated by repository or project, remains outside plugin caches, and survives plugin upgrades. `LEOS_AGENT_PATH` can redirect it.

## Repository layout

```text
.claude-plugin/marketplace.json       Claude marketplace catalog
.agents/plugins/marketplace.json     Codex marketplace catalog
.cursor-plugin/marketplace.json      Cursor marketplace catalog
plugin.yaml + __init__.py            Hermes plugin entrypoint
plugins/leo/                          self-contained cached plugin payload
  .claude-plugin/plugin.json
  .codex-plugin/plugin.json
  .cursor-plugin/plugin.json
  config/models.json                  canonical model matrix
  roles/                              canonical role prompts
  adapters/                           generated harness agent definitions
  skills/ hooks/ scripts/ workflows/
tests/                                stdlib packaging and behavior tests
```

Nothing in `plugins/leo/` depends on files outside that directory. This matters because plugin systems copy or cache the payload independently of the marketplace repository.

## Development and release

Run the complete local checks with:

```sh
python3 plugins/leo/scripts/render_adapters.py --check
python3 -m unittest discover -s tests -v
claude plugin validate .
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/leo
```

Version `4.0.0` is aligned across the three plugin manifests and Hermes manifest. A future `vX.Y.Z` tag triggers the release workflow, which verifies version alignment, runs the suite, builds the generic and Hermes archives, and publishes a GitHub release. Creating or pushing the tag remains a deliberate maintainer action.

For local harness testing, point each harness's development-plugin facility at `plugins/leo/`; test Hermes from the repository root because its entrypoint wraps the nested payload.
