# Leo's Agent

Leo's Agent is a portable agent operating policy for Claude Code, Codex, Cursor, Hermes, and OpenCode. It packages cost-tiered routing across model *and* reasoning effort, seven specialist roles, process skills, a review gate, and a narrow catastrophic-command guard as native plugins.

Nothing is injected. Every instruction reaches the model through a mechanism the harness loads on its own — a skill, an agent definition, or the one hook that guards destructive shell commands. Reading the policy costs nothing until something asks for it.

The repository does not need to be cloned for normal use. Each harness installs Leo's Agent through its own plugin system, and updates come through that system.

Leo supports macOS, Linux, and WSL with Python 3.9 or newer available to the
harness. Native Windows is unsupported. Before enabling any plugin hooks,
review the hook commands and grant trust only when you are comfortable with
their local effects; in Codex, use `/hooks` to review and trust Leo's hooks.

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

Start a new session after installing or updating. Claude loads the plugin's
native agents, skills, and the guard hook from the cache. `plugins/leo/settings.json`
is a reference configuration for Leo, not a plugin component; apply any values
you want yourself.

Verify the installed version and component inventory with `claude plugin list` and `claude plugin details leo@leos-agent`. That second command also reports the plugin's always-on token cost, which is the number this project is built around keeping small.

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

Start a new task after installing or updating. Codex loads the skills and the guard hook; Leo's Agent dispatches generic subagents with an explicit role prompt, model, and reasoning effort instead of installing global agent TOMLs.

Verify that `leo@leos-agent` is installed with:

```sh
codex plugin list --json
```

Then review Leo's hook commands in `/hooks`, start a new task after granting
trust, and invoke `leo:routing` to confirm the policy is reachable.

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

Hermes installs the Git repository into its plugin directory and loads the root
`plugin.yaml` and `__init__.py` entrypoint — it is the one harness whose plugin
root is this repository rather than `plugins/leo/`. On load, the entrypoint
registers Leo's portable skills as `leo:<skill>` and installs the
catastrophic-command guard, and nothing else. No policy is injected: start with
`leo:routing`, the same as everywhere else.

Verify the enabled state with `hermes plugins list`; inside a running session, `/plugins` shows the loaded plugin.

### OpenCode

OpenCode has no GitHub-based plugin marketplace — the plugin is distributed on npm as `leos-agent` and installed by module name:

```sh
opencode plugin leos-agent --global
```

That resolves the package and adds it to the global config for you. On OpenCode builds without the `plugin` subcommand, add it by hand instead — the global config file is `~/.config/opencode/opencode.json` or `opencode.jsonc`:

```json
{ "$schema": "https://opencode.ai/config.json", "plugin": ["leos-agent"] }
```

Start a new OpenCode session after installing. On startup the plugin registers a generated shadow copy of the skills directory, seven generated `leo-<role>` subagent roles, and the bash deletion tripwire. It writes no instructions of its own. A pre-existing user agent with the same namespaced key is preserved and reported rather than overwritten.

Configure and authenticate the OpenRouter provider before using the mapped
models: run `opencode auth login` and choose OpenRouter. Leo does not receive,
store, or write provider credentials. Update with
`opencode plugin leos-agent --global --force`, then start a new session.
OpenCode currently has no plugin removal command; remove the `"leos-agent"`
entry from the resolved configuration to uninstall.

OpenCode names each skill from its own frontmatter, requires that name to match the containing directory, and applies no plugin namespace, so there is no `leo:` prefix to register directly. Instead the plugin builds a shadow copy of the skills directory with every skill's directory and frontmatter `name:` renamed to `leo-<name>` and registers that copy, so skills are invoked as `leo-routing` and `leo-verification` rather than `leo:routing`. The harness reference says to read every `leo:<skill>` as `leo-<skill>` here.

If skills don't appear, run `opencode debug skill` — every Leo skill should
list a `location` under the machine-local state root
(`${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/opencode-skills-<hash>/leo-<name>/`).
Those are generated shadow skills, not the installed package. The generated
agents are registered from `adapters/opencode/agents.json`; `opencode debug
config` shows the resolved skill paths and agents. The plugin derives and
writes only these Leo-owned registrations. Do not hand-write its paths; if
they are absent, the plugin did not load.

## Model tiers

Tier names describe work, not a universal provider model. A tier is a model **and** a reasoning effort, so escalating a rung buys a wider thinking budget rather than only a bigger model. The canonical defaults live in [`plugins/leo/config/models.json`](plugins/leo/config/models.json).

| Tier | Typical work | Claude Code | Codex | Cursor | OpenCode via OpenRouter |
|---|---|---|---|---|---|
| Opus | Planning, investigation, review | `opus`, high | `gpt-5.6-sol`, high | Grok 4.5 | `moonshotai/kimi-k3` |
| Sonnet | Implementation | `sonnet`, medium | `gpt-5.6-terra`, medium | Grok 4.5 | `z-ai/glm-5.2` |
| Haiku | Exploration and mechanical work | `haiku`, low | `gpt-5.6-terra`, low | Composer 2.5 | `z-ai/glm-5.2` |

The role mapping is planner, investigator, and reviewer → Opus; implementer and review-lens → Sonnet; executor and explore → Haiku.

Only Claude Code and Codex can pin effort. Cursor agents are `model: inherit` with no effort control, and OpenCode pins a model per registered agent but no effort — on both, a tier is a recommendation, and the harness reference says so rather than implying a pin that does not exist.

There is no rung above Opus. Escalation caps there and reports, instead of handing a stuck question sideways.

### Change model defaults

- Maintainers edit only `plugins/leo/config/models.json`, then run `python3 plugins/leo/scripts/render_adapters.py`. CI runs the same command with `--check` to reject generated-file drift.
- Claude tiers are not configurable per install. Claude Code does not interpolate plugin options into agent frontmatter, so model and effort are baked into the generated agents: retier by editing the config, re-running the renderer, bumping the plugin version, and running `claude plugin update leo@leos-agent`. The version bump matters — the plugin cache keys on it, so an unbumped edit never reaches an installed plugin.
- Claude agent models are bare aliases, never `opus[1m]`. Agent frontmatter accepts an alias, a full model id, or `inherit`; the extended-context suffix is `/model` syntax and belongs only in *skill* frontmatter, which is why the Claude-only skill still carries it. The renderer now rejects a suffixed model outright.
- `plugins/leo/settings.json` is a reference copy of Leo's own Claude Code settings, not a plugin component — no harness loads it. Apply those values by hand if you want them.
- Codex users can override a model for one request in the prompt, or persist a tier override in native `AGENTS.md`. Explicit user instructions take precedence over bundled defaults.
- Cursor users select the mapped model in the native model picker before starting a homogeneous tier batch. Generated Cursor agents use `model: inherit`.
- OpenCode collapses Sonnet and Haiku onto one model. Moving between collapsed rungs changes which role does the work, not how much thinking it gets.

## What the plugin provides

- `leo:routing`: the operating policy — tiering, delegation economy, fan-out authorisation, machine-local state, and the index of every other skill. An ordinary skill, loaded on demand.
- `leo:review-gate`: what counts as reviewed before a change may be called done, the two exemptions, and the rubric a verdict is judged on. It lives in a skill rather than inside one role's prompt so the gate survives a harness with no reviewer agent and no bundled review skill.
- Seven roles: planner, investigator, reviewer, review-lens, implementer, executor, and explore. Which of them a harness registers depends on what it already has — see below.
- Process skills: `brainstorming`, `writing-plans`, `executing-plans`, `debugging`, `test-first`, `verification`, `delegation`, `worktrees`, `visual-verification`, `freshness`, and `finishing-a-branch`.
- Operational skills, portable to every harness: `resolve-ticket`, `review-pr`, and `watch-review`. `attach-pr` alone stays Claude Code only — its entire product is a side effect in Claude Code Desktop's PR-card detector, so elsewhere the same commands would succeed and produce nothing observable. It ships from a separate `skills-claude/` root that the Cursor, Codex, and OpenCode manifests do not read.
- A shared bash guard that blocks a narrow class of accidental home/system-scale destructive commands. It is the only hook Leo ships.

The bash guard is an accident-prevention tripwire, not an adversarial shell sandbox. It deliberately does not try to enumerate every obfuscation or malicious-command technique; each harness's permissions and sandbox remain the security boundary.

### Native substitutions

Leo does not ship a second copy of something the harness already does well. On Claude Code, `explore`, `planner`, `reviewer`, and `review-lens` are not registered at all — the built-in Explore and Plan agents and `/code-review` cover them — and `leo:worktrees` and `leo:visual-verification` ship as reference while the policy points at `EnterWorktree`/`ExitWorktree` and `/run`/`/verify`. The other three harnesses have none of those natives and get Leo's own versions.

Every substitution is recorded in `config/models.json` with the native it defers to and the reason, and rendered into the generated harness reference, so a session can see what is installed here and what to reach for instead. A substitution that names a native missing from your install falls back to Leo's version.

Skills can only be hard-excluded where packaging allows it. The Codex validator reads a single `skills` directory and Cursor's requires one directory, so only Claude's array-valued manifest and OpenCode's own tree walk can leave a skill out; elsewhere a substitution ships the skill and points at the native instead. The renderer refuses to record an exclusion the build cannot deliver.

## MCP integrations

Leo's Agent bundles no MCP servers, installs nothing, and holds no credentials. Register whatever servers you want through each harness's own configuration — `claude mcp add`, `codex mcp add`, Cursor's `~/.cursor/mcp.json`, or OpenCode's config file. Leo's skills use the tools they find and document a fallback when a server is absent.

That separation keeps workflow policy apart from personal services, credentials, and organization-specific access.

## Machine-local state

Skills that persist state write JSON under:

```text
${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/<skill-or-agent-name>.json
```

`~/.leos-agent-local` is a dedicated data directory, not an installation clone, so there's no nested `local/` segment inside it. State is separated by repository or project, remains outside plugin caches, and survives plugin upgrades. `LEOS_AGENT_LOCAL_PATH` can redirect it.

Where the harness owns a task list of its own, that list is the better ledger for in-flight progress, and the policy says to use it rather than keeping a second one alongside.

## Uninstall and recovery

Uninstalling a harness plugin removes its cached plugin payload but preserves
machine-local state. This is intentional: an update or reinstall should not
erase a ledger mid-run. To remove Leo completely, first export or copy
`${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/` somewhere safe, uninstall
the plugin from each harness, then explicitly remove that state directory.
Review the target carefully: full purge is manual and irreversible.

For the 8.0 upgrade: the memory store, `leo:setup`, and `leo:doctor` are gone,
and so is the injected policy block. Start with `leo:routing`, which is an
ordinary skill you or the model can invoke. Durable facts you kept in Leo's
memory store belong in the harness's own memory surface — `~/.claude/CLAUDE.md`,
`~/.codex/AGENTS.md`, a Cursor rules file — which is where they were being
copied to anyway; move anything you want to keep before removing the old
`memory/` directory. Ledgers under the state root are untouched. If Codex
policy is still absent, revisit `/hooks` and confirm trust.

## Repository layout

```text
.claude-plugin/marketplace.json      Claude marketplace catalog
.agents/plugins/marketplace.json     Codex marketplace catalog
.cursor-plugin/marketplace.json      Cursor marketplace catalog
plugin.yaml __init__.py              Hermes plugin root: manifest and entrypoint
.github/workflows/                   CI and release automation
plugins/leo/                          self-contained cached plugin payload, also published to npm as leos-agent
  .claude-plugin/plugin.json
  .codex-plugin/plugin.json
  .cursor-plugin/plugin.json
  package.json                        npm manifest for the OpenCode distribution
  README.md                           generated npm landing page (not a copy of this file)
  config/models.json                  canonical matrix: tiers, roles, capabilities, native substitutions
  roles/                              canonical role prompts, carrying no model or effort
  agents/                             generated Claude agents (conventional path)
  adapters/                           generated agent definitions for other harnesses
  adapters/opencode/                  OpenCode plugin.js bridge + generated agents.json
  skills/                             portable skills (every harness)
  skills/routing/references/          generated harness reference, one section per harness
  skills-claude/                      attach-pr (Claude Code only)
  hooks/ scripts/ workflows/          the guard, support scripts, and reusable workflow features
tools/                                release and pinned validation tooling
  release.py                          version checks, archives, npm staging, and GitHub Release sync
  vendor/                             pinned Codex and Cursor validators
local/                                ignored maintainer-only snapshots; never shipped
tests/                                stdlib packaging and behavior tests
```

Nothing in `plugins/leo/` depends on files outside that directory. This matters because plugin systems copy or cache the payload independently of the marketplace repository.

## Development and release

Run the complete local checks with:

```sh
python3 plugins/leo/scripts/render_adapters.py --check
python3 -m unittest discover -s tests -v
claude plugin validate .
python3 tools/vendor/codex/validate_plugin.py plugins/leo
node tools/vendor/cursor/validate-template.mjs
```

The vendored validators are pinned and documented in
[`tools/vendor/VALIDATORS.md`](tools/vendor/VALIDATORS.md); do not replace
them with a mutable download. `tools/release.py` verifies manifest alignment,
builds a reproducible archive, stages the npm package, and syncs the GitHub
Release. A `vX.Y.Z` tag triggers the release workflow, which runs those steps
and publishes `plugins/leo` to npm as `leos-agent` through Trusted Publishing
/ OIDC. Creating or pushing the tag remains a deliberate maintainer action.

Release history and generated notes live in
[GitHub Releases](https://github.com/foxhatleo/leos-agent/releases); this
repository intentionally has no hand-maintained changelog.

Contributor guidance, including how to author a skill in this shape and the
listing-text budget every skill is held to, is in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

For local harness testing, point each harness's development-plugin facility at `plugins/leo/`. For OpenCode, point the `plugin` array at the working tree instead of the npm package name:

```json
{ "plugin": ["/absolute/path/to/leos-agent/plugins/leo"] }
```
