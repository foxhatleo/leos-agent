# Leo's Agent

Leo's Agent is a portable agent operating policy for Claude Code, Codex, Cursor, Hermes, and OpenCode. It packages cost-tiered model routing, seven specialist roles, process skills, execute-then-review discipline, and a narrow catastrophic-command guard as native plugins.

The repository does not need to be cloned for normal use. Each harness installs Leo's Agent through its own plugin system, and updates come through that system.

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

Start a new session after installing or updating. Claude loads the plugin's native agents, skills, settings, and hooks from the cache.

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

Start a new task after installing or updating. Codex loads the skills and hooks from the plugin; Leo's Agent dispatches generic subagents with an explicit role prompt, model, and reasoning effort instead of installing global agent TOMLs.

Verify that `leo@leos-agent` is installed with `codex plugin list`.

### Cursor

Once Leo's Agent is listed in Cursor's public marketplace:

```text
/add-plugin leo
```

Before marketplace approval, install it directly from this public repository:

```text
/add-plugin leo@https://github.com/foxhatleo/leos-agent
```

Use Cursor's Customize → Plugins screen to verify, update, disable, or remove it. Cursor agents inherit the model selected in the UI; Leo's Agent recommends a tier but does not claim to enforce an arbitrary model name per subagent.

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

Hermes installs the Git repository into its plugin directory and loads the root `plugin.yaml` and `__init__.py` entrypoint. Leo's Agent registers its skills as `leo:<skill>` and injects the routing policy before model calls.

Verify the enabled state with `hermes plugins list`; inside a running session, `/plugins` shows the loaded plugin.

### OpenCode

OpenCode has no GitHub-based plugin marketplace — the plugin is distributed on npm as `leos-agent` and installed by module name:

```sh
opencode plugin leos-agent --global
```

That resolves the package and adds it to the global config for you. On OpenCode builds without the `plugin` subcommand, add it by hand instead — the config file is `~/.config/opencode/opencode.json` or `opencode.jsonc` (OpenCode loads `config.json`, `opencode.json`, and `opencode.jsonc`, in that order):

```json
{ "$schema": "https://opencode.ai/config.json", "plugin": ["leos-agent"] }
```

Start a new OpenCode session after installing. On startup the plugin registers the skills directory, the six generated subagent roles, and the using-leo policy (injected through `config.instructions` and, as a fallback, the chat system-prompt transform), and installs the bash deletion tripwire.

OpenCode names skills from their own frontmatter and applies no plugin namespace, so they are invoked as `brainstorming` and `verification` rather than `leo:brainstorming`. The harness mapping tells the agent to read `leo:<skill>` in the policy as `<skill>` here.

If skills don't appear, run `opencode debug skill` — every Leo skill should list a `location` inside the installed package. `opencode debug config` shows the resolved `skills.paths`, `instructions`, and `agent` entries. The plugin derives its own install location and registers all three itself, so no path ever needs to be written by hand; if they are missing, the plugin did not load at all.

Remove it by deleting the `"leos-agent"` entry from the `plugin` array.

## Model tiers

Tier names describe work, not a universal provider model. The canonical defaults live in [`plugins/leo/config/models.json`](plugins/leo/config/models.json).

| Tier | Typical work | Claude Code | Cursor | Codex | Hermes via OpenRouter | OpenCode via OpenRouter |
|---|---|---|---|---|---|---|
| Fable | Expert arbitration | `fable` | GPT-5.6 Sol | `gpt-5.6-sol`, max | `moonshotai/kimi-k3` | `moonshotai/kimi-k3` |
| Opus | Planning, investigation, review | `opus` | Grok 4.5 | `gpt-5.6-sol`, high | `moonshotai/kimi-k3` | `moonshotai/kimi-k3` |
| Sonnet | Implementation | `sonnet` | Grok 4.5 | `gpt-5.6-terra`, medium | `z-ai/glm-5.2` | `z-ai/glm-5.2` |
| Haiku | Exploration and mechanical work | `haiku` | Composer 2.5 | `gpt-5.6-luna`, low | `z-ai/glm-5.2` | `z-ai/glm-5.2` |

The role mapping is expert → Fable; planner, investigator, and reviewer → Opus; implementer → Sonnet; executor and explore → Haiku.

### Change model defaults

- Maintainers edit only `plugins/leo/config/models.json`, then run `python3 plugins/leo/scripts/render_adapters.py`. CI runs the same command with `--check` to reject generated-file drift.
- Claude tiers are not configurable per install. Claude Code does not interpolate plugin options into agent frontmatter, so the models are baked into the generated agents: retier by editing `plugins/leo/config/models.json`, re-running the renderer, bumping the plugin version, and running `claude plugin update leo@leos-agent`. The version bump matters — the plugin cache keys on it, so an unbumped edit never reaches an installed plugin.
- Claude agent models are bare aliases, never `opus[1m]`. Agent frontmatter accepts an alias, a full model id, or `inherit`; the extended-context suffix is `/model` syntax and belongs only in *skill* frontmatter, which is why the Claude-only skills still carry it. On plans where Opus already runs with the larger context window, the suffix bought nothing in agents anyway.
- `plugins/leo/settings.json` is a reference copy of Leo's own Claude Code settings, not a plugin component — no harness loads it. Apply those values by hand if you want them.
- Codex users can override a model for one request in the prompt, or persist a tier override in native `AGENTS.md`. Explicit user instructions take precedence over bundled defaults.
- Cursor users select the mapped model in the native model picker before starting a homogeneous tier batch. Generated Cursor agents use `model: inherit`.
- Hermes users switch the parent with `/model` and configure one delegation model for all native children. A delegation batch cannot mix Kimi and GLM.
- OpenCode tiers, like Hermes, have no Fable rung — Fable and Opus collapse onto `moonshotai/kimi-k3`, so `expert` is not registered as an agent on this harness. `reviewer` always runs the full Opus-tier model here; there is no per-spawn downscale.

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
- Evidence and upkeep skills, portable to every harness: `freshness` (confirm a third-party API against the installed package before coding against it), `visual-verification` (a change someone can see needs a render, or an explicit unverified warning), `memory` (durable cross-harness facts), `doctor` (report how the plugin is wired here), `writing-skills` (author a skill in this shape, and where a personal one goes per harness), and `setup` (turn on opt-in wiring a plugin install cannot turn on for itself).
- Operational skills, portable to every harness: `resolve-ticket`, `review-pr`, and `watch-review`. `attach-pr` alone stays Claude Code only — its entire product is a side effect in Claude Code Desktop's PR-card detector, so elsewhere the same commands would succeed and produce nothing observable. It ships from a separate `skills-claude/` root that the Cursor, Codex, Hermes, and OpenCode manifests do not read, and each harness's mapping appendix says so.
- Session bootstrap hooks for Claude Code, Codex, and Cursor, plus native policy injection for Hermes and OpenCode.
- A shared bash guard that blocks a narrow class of accidental home/system-scale destructive commands.

The bash guard is an accident-prevention tripwire, not an adversarial shell sandbox. It deliberately does not try to enumerate every obfuscation or malicious-command technique; each harness's permissions and sandbox remain the security boundary.

## MCP integrations

Leo's Agent does not bundle MCP servers. Install and authenticate Linear, Slack, Atlassian, Google, Vercel, Notion, or other MCP integrations independently through the harness that will use them. This keeps the workflow policy separate from personal services, credentials, and organization-specific access.

## Machine-local state

Skills that persist state write JSON under:

```text
${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/<skill-or-agent-name>.json
```

`~/.leos-agent-local` is a dedicated data directory, not an installation clone, so there's no nested `local/` segment inside it. State is separated by repository or project, remains outside plugin caches, and survives plugin upgrades. `LEOS_AGENT_LOCAL_PATH` can redirect it.

## Memory

Durable facts are separate from that per-task JSON. They live one fact per markdown file under:

```text
${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/memory/
```

with a `global/` scope and a `repo/<slug>/` scope, plus a generated index. That store is the only writable copy; read and write it through `python3 <plugin-root>/scripts/memory.py` (`write` / `list` / `read` / `forget`).

Each harness's own per-user memory surface then receives a generated copy of the **global** facts — `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.config/opencode/AGENTS.md`, and a Cursor rules file — so a preference learned on one harness is present on the next. Repo-scoped facts are deliberately never projected: every one of those surfaces loads in every repository, so projecting them would leak one project's memories into unrelated sessions. Repo facts reach the model through the session-start context block, which knows the working directory.

Projection only ever rewrites the text between its own `<!-- BEGIN leos-agent memory -->` and `<!-- END leos-agent memory -->` markers; everything outside them is preserved byte for byte, and the file is copied once to `<file>.leo-backup` before the first write. A directory that does not already exist is never created, so a harness you have not installed is left alone. Set `LEOS_AGENT_NO_PROJECT=1` to disable writing to native surfaces entirely. Repository-tracked `CLAUDE.md` and `AGENTS.md` files are never touched, so memories never land in git.

**Upgrading to 6.0.0.** Before 6.0.0 this was `LEOS_AGENT_PATH`, and state lived one level deeper, under a nested `local/`. The old variable is no longer read: if it is still set it is ignored silently rather than erroring, so state at the old location becomes invisible instead of failing loudly. That is deliberate — a hard error here would break every session, including the hooks on the failure path — but it means the move is manual. Copy any `*.json` from the old `<old-path>/local/` into `${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/`, and rename the variable wherever you set it.

## Repository layout

```text
.claude-plugin/marketplace.json       Claude marketplace catalog
.agents/plugins/marketplace.json     Codex marketplace catalog
.cursor-plugin/marketplace.json      Cursor marketplace catalog
plugin.yaml + __init__.py            Hermes plugin entrypoint
plugins/leo/                          self-contained cached plugin payload, also published to npm as leos-agent
  .claude-plugin/plugin.json
  .codex-plugin/plugin.json
  .cursor-plugin/plugin.json
  package.json                        npm manifest for the OpenCode distribution
  README.md                           generated npm landing page (not a copy of this file)
  config/models.json                  canonical model matrix
  roles/                              canonical role prompts
  agents/                             generated Claude agents (conventional path)
  adapters/                           generated agent definitions for other harnesses
  adapters/opencode/                  OpenCode plugin.js bridge + generated agents.json
  skills/                             portable skills (every harness)
  skills-claude/                      attach-pr (Claude Code only)
  hooks/ scripts/ workflows/
tests/                                stdlib packaging and behavior tests
```

Nothing in `plugins/leo/` depends on files outside that directory. This matters because plugin systems copy or cache the payload independently of the marketplace repository.

## Development and release

Run the complete local checks with:

```sh
python3 plugins/leo/scripts/render_adapters.py --check
python3 -m unittest discover -s tests -v
claude plugin validate .
curl -fsSL https://raw.githubusercontent.com/openai/codex/main/codex-rs/skills/src/assets/samples/plugin-creator/scripts/validate_plugin.py -o /tmp/validate_plugin.py
python3 /tmp/validate_plugin.py plugins/leo
```

Version `6.2.0` is aligned across the three plugin manifests, the Hermes manifest, and `plugins/leo/package.json`. A future `vX.Y.Z` tag triggers the release workflow, which verifies version alignment, runs the suite, builds the generic and Hermes archives, publishes a GitHub release, and publishes `plugins/leo` to npm as `leos-agent` (Trusted Publishing / OIDC — no stored token). Creating or pushing the tag remains a deliberate maintainer action.

For local harness testing, point each harness's development-plugin facility at `plugins/leo/`; test Hermes from the repository root because its entrypoint wraps the nested payload. For OpenCode, point the `plugin` array at the working tree instead of the npm package name:

```json
{ "plugin": ["/absolute/path/to/leos-agent/plugins/leo"] }
```
