# leos-agent

Cost-aware delegation and portable workflows for Claude Code, Codex, Cursor,
OpenCode, Hermes, and Pi. Version **12.2026090800.0**.

The main agent handles small work directly and delegates substantial, bounded
work to the cheapest competent tier. The policy separates **whether to delegate**
from **which model to use**. Native adapters check model costs where the harness
exposes enough information, while skills load detailed procedures on demand.

This is a useful direction when expensive parents otherwise do large amounts of
routine work or spawn expensive children by inheritance. It can cost more when
briefing, duplicated context, verification, and retries exceed the work saved.
There is no claim of universal token reduction or measured savings without a
comparable task baseline. Optimizing dollars can mean spending more tokens on
a cheaper model while preserving quality.

## Routing and cost

| Tier | Appropriate work | Claude default | Codex default |
|---|---|---|---|
| Cheap | Bounded factual checks, mechanical work, known procedures | Haiku | GPT-5.6 Luna |
| Standard | Investigation, implementation, diagnosis, ordinary review | Sonnet | GPT-5.6 Terra |
| Parent-level | Work requiring the parent's capability | Current parent | Current parent |

Native profiles are `leo-cheap`, `leo-standard`, `leo-parent`, and
`leo-reviewer`. `leo-runner` and `leo-executor` remain legacy aliases. Review
may use nested read-only lenses; ordinary workers do not delegate. Small
reviews run locally, and larger reviews divide independent areas rather than
requiring every lens to reread everything.

The tier name is not a price ordering. For example, the bundled reference
catalog prices Terra output above Sol output; the ceiling therefore replaces
that selection with the current parent where supported. Input/output crossover
rates, unknown IDs, and ambiguous catalog matches are allowed with diagnostics,
as configured by this project's policy. Thus the ceiling prevents **known**
overselection, not every possible billing outcome.

Prices come from the public [OpenRouter model catalog](https://openrouter.ai/docs/guides/overview/models),
including Claude, GPT, DeepSeek, Kimi, GLM, and Qwen families. Exact IDs and
recognized aliases are preferred. Nearby versions of the same variant can use
an explicitly labeled estimate; sizes, cheap/pro variants, and free endpoints
are not conflated. A price alias never changes the identifier sent to a harness.
Public prices do not establish account access, model availability, negotiated
rates, or subscription-credit accounting.

Refresh runs in ordinary code at installation/update and, when used, at most
daily. It is bounded and runs separately from dispatch. Failed refreshes keep
the last catalog and record a diagnostic. No dispatch performs network I/O or
asks a model to look up prices. Set `LEOS_AGENT_PRICE_REFRESH=off` for offline
operation.

## Harness capabilities

| Harness | Policy delivery | Model control | Important limit |
|---|---|---|---|
| Claude Code | SessionStart, including forks | Agent/Task argument correction; native profiles | Forced settings and provider/org substitutions can affect execution; observe child transcripts. |
| Codex | Separate native SessionStart hook | Tier-enforcing explicit spawn selection; model-free native profiles | Hooks need native trust. Other/customized profiles can still override spawn settings. |
| Cursor | Native always-apply rule | Installed user agents; resolved subagentStart model ceiling | No invented Task model argument; hook diagnostics distinguish planned models from completion. |
| OpenCode | One registered rendered instruction | Native agent selection, confirmed through the SDK | Task has no model field; source/config paths must remain valid. |
| Hermes | Frozen system-prompt section | Global native delegation-model ceiling when parent/model are observable | Native delegation has one global model, not separate per-task tiers. |
| Pi | Extension caches rendered body per session | Advisory policy and extension-dependent dispatch checks | No native per-spawn model guarantee for third-party subagent tools. |

Portable skills are registered on all six. Native capability differences are
reported rather than presented as full enforcement parity. Policy, pricing,
routing decisions, and installation rendering share ordinary code; thin
adapters implement only supported controls. See [hook contracts](hooks/README.md).

## Install and upgrade

Requires macOS or Linux, Python 3.9+, and a current public harness. GitHub workflows need
authenticated `gh`. JavaScript harnesses supply their own Node/Bun runtime.
No OpenAI or Anthropic marketplace submission is needed: use this repository
as your own plugin source or a local checkout.

Install **only the harness you intend to configure**. Native plugin discovery
and the integration installer are separate steps. Run the installer again
after upgrades or model-mapping changes, because native profiles and copied
OpenCode resources may need refreshing.

### Claude Code

```sh
claude plugin marketplace add foxhatleo/leos-agent
claude plugin install leos-agent@leos-agent --scope user
```

Then invoke the plugin's `install` skill in Claude. To upgrade, refresh the
marketplace and plugin through Claude's plugin manager, then run `install`
again and start a new session. Third-party marketplace auto-update settings may
be disabled; do not assume every client updates itself.

### Codex

```sh
codex plugin marketplace add foxhatleo/leos-agent
codex plugin add leos-agent@leos-agent
```

Invoke the plugin's `install` skill to install native agent TOMLs. Review the
current hook definitions in `/hooks`; enabling a plugin does not trust its
hooks automatically. On upgrade, refresh the marketplace/plugin, rerun
`install`, and review any changed hook definition. The project does not bypass
that native trust boundary.

### Cursor

Add this repository through Cursor's plugin UI, or use its local-plugin setup:

```sh
git clone https://github.com/foxhatleo/leos-agent ~/.cursor/plugins/local/leos-agent
python3 ~/.cursor/plugins/local/leos-agent/scripts/leo-install.py cursor
```

Confirm the plugin in Customize and inspect Hooks diagnostics. Set explicit
models available to your Cursor account, then rerun the installer. Profiles
are written under `~/.cursor/agents`; `~/.cursor/rules` is not assumed to be a
supported global rule location. Upgrade a local clone with `git pull --ff-only`,
rerun the installer, and reload the plugin.

### OpenCode

A stable checkout avoids versioned package-cache paths:

```sh
git clone https://github.com/foxhatleo/leos-agent ~/.local/share/leos-agent
python3 ~/.local/share/leos-agent/scripts/leo-install.py opencode
```

The installer registers the checkout's plugin URI, one rendered instruction,
native agent profiles, and skill/reference copies. It preserves JSONC comments
and unrelated configuration. Upgrade that checkout with `git pull --ff-only`
and rerun the same command. The npm package is also published as `leos-agent`;
if using a package cache, resolve its actual root and rerun the installer when
that path changes. Do not assume a fixed cache directory layout.

### Hermes

Use Hermes's local-plugin directory under your active HERMES_HOME/profile to
install this repository as `plugins/leos-agent`, then enable it through Hermes's
plugin manager. Run the registered `leo-install` command. The native plugin
registers portable skills, one frozen policy section, and dispatch diagnostics.
Hermes supports the policy for deciding whether to delegate, but not per-task
model tiers. The installer preserves its native delegation-model setting;
saved cheap/standard mappings are not applied. The guard can still check known
child/parent prices when both models are observable.

### Pi

```sh
pi install git:github.com/foxhatleo/leos-agent
```

Invoke the installed `install` skill for `pi`, then start a new session.
Package metadata provides skill discovery once; the extension does not register
the same skills a second time. Upgrade through Pi's package manager and rerun
`install`. A subagent extension is required for delegation; this project does
not pretend that every such extension accepts model overrides.

### Installer controls

From the resolved plugin root:

```sh
python3 scripts/leo-install.py <harness> --dry-run
python3 scripts/leo-install.py <harness> --check
python3 scripts/leo-install.py <harness> --rollback
python3 scripts/leo-install.py <harness> --uninstall
```

Normal installation stages and validates all changes first, writes a private
backup, and rolls back earlier writes if an operation fails. Rollback refuses
intervening edits and can recover a partially applied installation. Only owned
entries/files are managed. Unchanged legacy copies are recognized by complete
content hashes; edited or unrelated files are preserved. `--force` is for a
specific conflict you explicitly intend to replace.

Run `--uninstall` before removing the native plugin/source. It removes owned
integration artifacts, not routing preferences, handoffs, or logs. Supported
config overrides include CODEX_HOME, CLAUDE_CONFIG_DIR, HERMES_HOME,
PI_CODING_AGENT_DIR, OPENCODE_CONFIG_DIR, OPENCODE_CONFIG, and XDG_CONFIG_HOME.

## Per-machine model routing

Data lives in `~/.leos-agent-local`, or LEOS_AGENT_LOCAL_PATH, outside versioned
plugin caches. Configure concrete provider/harness IDs rather than assuming
that a familiar alias exists everywhere:

```sh
python3 scripts/routing.py set --harness claude --cheap haiku --standard sonnet
python3 scripts/routing.py set --harness codex --cheap gpt-5.6-luna --standard gpt-5.6-terra
python3 scripts/routing.py show
python3 scripts/leo-install.py <harness>
```

`routing.json` accepts `cheap` and `standard` as strings or objects containing
`model` and optional `effort`. Legacy `runner`/`executor` keys remain readable.
Unknown model identifiers are retained and diagnosed, not silently corrected
to a different dispatch ID. Parent-level always means the current parent.

Guard modes are `LEOS_AGENT_DISPATCH_GUARD=on` (default), `warn` (log proposed
corrections/blocks), and `off`. Unrelated tools and third-party MCP tools are
not routed. Failures are logged distinctly and fail open; this is not a
security boundary. Native or organization-level model substitutions may still
require investigation of actual execution.

## Workflows and diagnostics

| Skill | Purpose |
|---|---|
| install | Configure this harness's native artifacts; preview/check/uninstall/rollback. |
| doctor | Check installation, pricing, model references, and runtime evidence without paid calls. |
| tune-routing | Configure tiers and diagnose actual native model selection. |
| review-usage | Mechanical usage scan with reference-cost estimates and explicit gaps. |
| review-pr | Review a pinned GitHub PR; stage comments/replies as pending. |
| handoff / handon | Save concise context pointers and resume after checking drift. |
| watch-review | Claude Monitor watcher: code polls GitHub; models run only for eligible reviews. |
| attach-pr | Claude-specific attachment of an existing PR to the desktop session. |

Only small routing instructions and necessary metadata are always present.
Detailed review procedures remain in deferred reference files. Duplicate
slash-command wrappers are removed; native skills provide invocation. Explicit
invocation controls do not universally imply hidden metadata—measure both.

```sh
python3 scripts/doctor.py --harness claude --json
python3 scripts/usage_scan.py --since 7d --harness claude --json
python3 scripts/dispatch_log.py report
python3 scripts/measure_context.py --check
python3 scripts/pricing.py resolve claude-sonnet-5
```

Default policy bodies are about 1.8–2.0 KB, with a 2.2 KB component budget.
Measurement counts metadata separately and treats bytes/4 only as a rough
prose-token proxy. Harness wrappers, history, tools, cache behavior, and child
work are outside that static measurement. No assertion is made that instruction
overhead always pays for itself.

Usage scanning handles Claude streaming duplicates, Codex cumulative/cache
accounting, and OpenCode message-time usage. Cursor/Hermes/Pi usage schemas are
currently unsupported and reported that way. Reference prices are not bills;
unknown cache/model costs remain explicit. Compaction pre-context counts are
not discarded tokens. Requested/corrected/blocked dispatches are distinct from
observed child models. Missing/rotated logs do not prove a broken install.

PR review pins the full head SHA; it never silently moves line anchors or
retries old findings against a new head. Replacing a pending review requires
an unchanged ownership receipt and saves recovery data. New comments/replies
remain pending. Only verified addressed threads rooted by the authenticated
user can be auto-resolved; resolution is public and requires SHA-bound evidence.
Watchers use cross-process leases, bounded retries, and completion reports;
emission alone never records a PR as reviewed.

Logs omit prompt text by default and rotate at 1 MiB plus one retained file.
`LEOS_AGENT_DISPATCH_LOG_PROMPTS=1` is an explicit debug option that retains
brief excerpts; avoid it for sensitive work. Installation backups and handoffs
stay local. Handoff names are validated, reservations avoid collisions, and
symlink escapes are refused by the helper.

## Development and releases

```sh
LEOS_AGENT_PRICE_REFRESH=off PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
node --test tests/js/*.test.js
python3 scripts/check.py
python3 scripts/measure_context.py --check
```

Tests use fixtures and mock provider events; CI makes no paid model calls.
Native live smoke tests are separate, explicitly authorized, and budgeted.
Static/adapter tests establish contracts, not complete end-to-end parity on
all six installed harnesses.

Commits run validation only. Release versions use `12.YYYYMMDDXX.0`, with a UTC
calendar date and a two-digit serial from 00 to 99. Run `scripts/bump.py` only
when preparing a release, validate all manifests/package contents, commit on
main, and publish the matching tag through the release workflow. npm publishing
uses its existing trusted-publishing workflow. An ordinary code commit never
automatically changes versions or stages unrelated files.

Native references: [Claude subagents](https://code.claude.com/docs/en/sub-agents),
[Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents),
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Cursor plugins](https://cursor.com/docs/reference/plugins),
[OpenCode plugins](https://opencode.ai/docs/plugins/),
[Hermes hooks](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/hooks.md),
[Pi extensions](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md).
