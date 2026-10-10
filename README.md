# leos-agent

Cost-aware delegation and portable workflows for Claude Code, Codex, Cursor,
OpenCode, Hermes, and Pi.

The main agent splits a task into steps, keeps small or dependent steps local,
and delegates independent or output-heavy steps to the cheapest tier whose
result a named check can verify. A failed check escalates one tier up; work
that cannot be checked stays local. Native adapters check model costs and
record how each worker's run ended where the harness exposes enough
information, while skills load detailed procedures on demand.

This is a useful direction when expensive parents otherwise do large amounts of
routine work or spawn expensive children by inheritance. It can cost more when
briefing, duplicated context, verification, and retries exceed the work saved.
There is no claim of universal token reduction or measured savings without a
comparable task baseline. Optimizing dollars can mean spending more tokens on
a cheaper model while preserving quality.

## Routing and cost

| Tier | Appropriate work | Claude default | Codex default |
|---|---|---|---|
| Cheap | Bounded factual checks, mechanical work, known procedures | Haiku | GPT-6 Luna |
| Standard | Investigation, implementation, diagnosis, ordinary review | Sonnet | GPT-6.1 Sol |
| Premium | Difficult diagnosis, cross-module design, complex implementation, consequential review | Opus | GPT-6 Astra |
| Parent-level | Exceptional work beyond premium capability that justifies delegation | Current parent | Current parent |

Native profiles are `leo-cheap`, `leo-standard`, `leo-premium`, `leo-parent`, and
`leo-reviewer`. The retired `leo-runner` and `leo-executor` profiles are gone,
but the guard and logs still map those names to cheap and standard, and routing
config still accepts its `runner` and `executor` keys. On Claude Code each
worker profile caps its turns; a capped run returns its output marked partial. Review
may use nested read-only lenses; ordinary workers do not delegate. Small
reviews run locally, and larger reviews divide independent areas rather than
requiring every lens to reread everything.

Delegation is decided by decomposition, not by guessing at size. A step is
delegated when it is independent of the parent's next step or when its tool
output would swell the parent's context; dependent chains stay local. Every
delegation names the check that proves its result. Workers end their reply with
`Result: done|partial|blocked|escalate` and `Verified: <evidence or none>`. When a
result fails its check or reports escalate, the parent re-dispatches one tier up
with a brief that begins `Escalation from <tier>:`; it repeats a tier only after
a transient tool error. `blocked` means the work needs a decision or permission,
so the parent asks for it instead of escalating.
Work that outlasts one context continues by handoff, not a deeper tree.

Choose tiers by ambiguity, consequence, and how reliably results can be checked.
Cheap handles retrieval, mechanical edits, and known checks; standard handles
ordinary implementation, tests, debugging, and scoped review. Premium handles
subtle failures, design tradeoffs across modules, complex changes, and reviews
where missed defects have substantial consequences. Parent-level delegation is
discouraged: use it only when premium is insufficient and substantial independent
work justifies a separate worker. Otherwise do that work in the parent.
The existing price ceiling applies to every tier; premium does not bypass it.

The bundled catalog puts the large price gap in the cheap tier. Under the usual
parents, Opus on Claude and Sol on Codex, premium costs the same as the parent
(Opus) or is capped to it (Astra is priced above Sol), and on Codex the standard
default is Sol itself. A child at the parent's price buys context isolation, not
a lower rate, so the policy keeps that work local unless isolation matters.
Cheap models also tend to spend more turns, so the cheap tier is for bounded work
with a mechanical check. The policy batches small steps of the same shape into
one dispatch and passes large briefs and diffs as file paths.

The tier name is not a price ordering. For example, the bundled reference
catalog prices GPT-6 Astra above GPT-6.1 Sol, so a premium Astra selection
under a GPT-6.1 Sol parent is replaced with the parent where supported. Input/output crossover
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

| Harness | Policy delivery | Model control | Outcome signal | Important limit |
|---|---|---|---|---|
| Claude Code | SessionStart, including forks | `agent.spawn` model setting on builds where it covers teammates too ([hooks/README.md](hooks/README.md#claude-agentspawn-mod)), else Agent/Task argument correction; native profiles | SubagentStop final message, child transcript fallback, child usage from transcript | On the Agent/Task path the first dispatch may precede parent transcript persistence; missing parent data permits dispatch with a diagnostic. Forced settings/provider substitutions also limit enforcement. |
| Codex | Separate native SessionStart hook | Tier-enforcing explicit spawn selection; model-free native profiles | SubagentStop final message, rollout fallback, cumulative token counts | Hooks need native trust. A renamed multi-agent tool namespace is not matched. Other/customized profiles can still override spawn settings. Encrypted briefs make the escalation marker unobservable; recorded as such. |
| Cursor | Native always-apply rule | Installed user agents; resolved subagentStart model ceiling | subagentStop summary and status, joined by call id; no usage | No invented Task model argument; hook diagnostics distinguish planned models from completion. Worker no-delegation is instruction-only: no per-agent tool restriction, and no parent-agent identity at subagentStart. |
| OpenCode | One registered rendered instruction | Native agent selection, confirmed through the SDK | `tool.execute.after` output of a foreground `task`, joined by call id, child model from task metadata; no usage | Task has no model field; a correction that cannot be applied blocks the task. A slash-command subtask's own model is not visible to the guard. Source/config paths must remain valid. |
| Hermes | Frozen system-prompt section | Global native delegation-model ceiling when parent/model are observable | `subagent_stop` per child (`post_tool_call` only when the build lacks it); no usage | Native delegation has one global model, not separate per-task tiers. Older builds skip post-tool hooks for built-in tools; the report then says no completion signal was observed. |
| Pi | Extension caches rendered body per session | Advisory policy; dispatches are logged without a model check | `tool_result` text and usage for a tool named `subagent` | No native per-spawn model guarantee for third-party subagent tools. |

Completion capture reads the last 4 KiB of a worker's final text, or of the
report a Claude child handed back through its hand-back tool, for its
`Result:` and `Verified:` lines and drops the text. On Claude Code and Codex, a
leo-* worker whose final message lacks those lines is asked once, at its first
SubagentStop, to restate its report with them; guard modes `warn` and `off`
skip the prompt. The dispatch log stores the outcome enum, a verified
tri-state, a source token, token and turn counts, the tier, and the escalation
source tier; never brief or result text. `dispatch_log.py report` joins
completions to dispatches by call id; on Claude, whose SubagentStop has none,
through a PostToolUse row linking each Agent call to the child it started;
and only without either, by the nearest preceding same-session dispatch of
the same tier. The report prints
outcome and verification counts per tier beside the dispatches that sent no
completion signal, escalation chains and tier counts over dispatches that ran,
summed child usage and turns, reference cost per verified success from catalog
prices (an estimate, not a bill), and which harnesses supplied no signal.
Some hosts send none for background children: Claude Code background subagents
on some builds and in the VS Code extension, OpenCode background tasks, and
Cursor background subagents. Claude Code's SessionEnd hooks share a 1.5 s budget that
plugin timeouts cannot raise, so late-transcript reconciliation runs in a
detached process.

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
and the integration installer are separate steps. The installer writes files
only for Codex, Cursor and OpenCode; on Claude Code, Hermes and Pi it only
removes a `<leos-agent>` block an older release left in the harness's global
instruction file, so on a fresh machine it has nothing to do there. Where it
writes files, run it again after upgrades or model-mapping changes. It needs
the harness's config directory to exist already (run the harness once), and
never creates one.

### Claude Code

```sh
claude plugin marketplace add foxhatleo/leos-agent
claude plugin install leos-agent@leos-agent --scope user
```

Start a new session; the plugin supplies agents, hooks and skills. If an older
release wrote a policy block into `~/.claude/CLAUDE.md`, run `/leos-agent:install`
once to take it out. To upgrade, refresh the marketplace and plugin through
Claude's plugin manager and start a new session. Third-party marketplace
auto-update settings may be disabled; do not assume every client updates itself.

### Codex

```sh
codex plugin marketplace add foxhatleo/leos-agent
codex plugin add leos-agent@leos-agent
```

Then run the install skill in a Codex session by mentioning `$leos-agent:install`
(or pick it from `/skills`). It is required here: it writes the cheap,
standard, premium, parent and reviewer agent TOMLs into `~/.codex/agents`.
Review the current hook definitions in `/hooks`; enabling a plugin does not
trust its hooks automatically. On upgrade, refresh the marketplace/plugin,
rerun `$leos-agent:install`, and review any changed hook definition. The
project does not bypass that native trust boundary.

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

Then restart OpenCode. The installer registers the checkout's plugin URI and
one rendered instruction in the active config (`OPENCODE_CONFIG`, else
`opencode.jsonc`, else `opencode.json`), and copies native agent profiles and
skill/reference copies with the checkout's absolute path baked in. It preserves
JSONC comments and unrelated configuration, and uninstall edits the same file
the install did. Inside OpenCode the install skill is `/leo-install`. Upgrade the
checkout with `git pull --ff-only`, rerun the same command, and restart.

The npm package is also published as `leos-agent`:

```sh
opencode plugin leos-agent --global
root="${XDG_CACHE_HOME:-$HOME/.cache}/opencode/packages/leos-agent/node_modules/leos-agent"
python3 "$root/scripts/leo-install.py" opencode
```

Run from OpenCode's package cache, the installer keeps the `leos-agent`
package entry rather than linking into the cache, so OpenCode can fetch the
package again after cleaning it. The skill copies still point into the cache:
rerun the installer whenever that directory changes (a pinned
`leos-agent@<version>` spec gets its own directory).

### Hermes

```sh
hermes plugins install foxhatleo/leos-agent --enable
```

This installs into `$HERMES_HOME/plugins/leos-agent` (default
`~/.hermes/plugins/leos-agent`). For a local checkout instead, clone it there and
run `hermes plugins enable leos-agent`. Start a new session. The native plugin
registers portable skills, one frozen policy section, dispatch diagnostics, and
the `/leo-install` command; that command only removes a block an older release
left in `SOUL.md`. Hermes supports the policy for deciding whether to delegate,
but not per-task model tiers. The installer preserves its native
delegation-model setting; saved cheap/standard/premium mappings are not applied.
The guard can still check known child/parent prices when both models are
observable.

### Pi

```sh
pi install git:github.com/foxhatleo/leos-agent
```

Start a new session. Package metadata provides skill discovery once; the
extension does not register the same skills a second time. If an older release
wrote a policy block into `~/.pi/agent/AGENTS.md`, run `/skill:install` once to
take it out. Upgrade through Pi's package manager. A subagent extension is
required for delegation; this project does not pretend that every such
extension accepts model overrides.

### Installer controls

From the resolved plugin root:

```sh
python3 scripts/leo-install.py <harness> --dry-run
python3 scripts/leo-install.py <harness> --check
python3 scripts/leo-install.py <harness> --rollback
python3 scripts/leo-install.py <harness> --uninstall
```

Normal installation stages and validates all changes first, writes a private
backup to `~/.leos-agent-local/install-backups/<harness>.json`, and rolls back
earlier writes if an operation fails; a failed run leaves the previous backup in
place. `--rollback` undoes the last install or uninstall, refuses intervening
edits, and can recover a partially applied installation.

Ownership is by content, not by the "Managed by leos-agent" header. Each install
records the sha256 of every file it writes in `leos-agent-paths.json` in the
harness config directory, and a file is replaced or removed only while it
still matches that receipt, this release's copy, or a copy an earlier release
wrote. On install, an edited copy or a file leos-agent never wrote is a
conflict that stops the run (`--force` replaces that file); an edited copy from
a release older than v12 is reported as `preserved` and the rest continues. On
uninstall, anything not provably ours is `preserved`, and `--force` does not
change that. Copies of the retired `leo-runner` and `leo-executor` profiles are
removed when unchanged and preserved otherwise.

Run `--uninstall` before removing the native plugin/source. It removes owned
integration artifacts and registrations, and deletes an OpenCode config file
only if the install created it and nothing else is left in it. It keeps
routing preferences, handoffs, logs, and its backup. Supported config overrides
are CODEX_HOME, CLAUDE_CONFIG_DIR, HERMES_HOME, PI_CODING_AGENT_DIR,
OPENCODE_CONFIG_DIR, OPENCODE_CONFIG, and XDG_CONFIG_HOME; each must be an
absolute path, and an empty one counts as unset.

## Per-machine model routing

Data lives in `~/.leos-agent-local`, or LEOS_AGENT_LOCAL_PATH, outside versioned
plugin caches. Configure concrete provider/harness IDs rather than assuming
that a familiar alias exists everywhere. Claude is the exception: its Agent
tool accepts only `haiku`, `sonnet`, `opus`, or `fable`, so `routing.py`
refuses any other Claude model:

```sh
python3 scripts/routing.py set --harness claude --cheap haiku --standard sonnet
python3 scripts/routing.py set --harness codex --cheap gpt-6-luna --standard gpt-6.1-sol
python3 scripts/routing.py show
python3 scripts/leo-install.py <harness>
```

`routing.json` accepts `cheap` and `standard` as strings or objects containing
`model` and optional `effort`. Legacy `runner`/`executor` keys remain readable.
Unknown model identifiers are retained and diagnosed, not silently corrected
to a different dispatch ID. Parent-level always means the current parent.

Guard modes are `LEOS_AGENT_DISPATCH_GUARD=on` (default), `warn` (log proposed
corrections/blocks), and `off`; `0`, `false`, `no`, and `disabled` also mean
off. Any other value keeps the guard on and marks each logged dispatch with an
`unrecognized-guard-mode` diagnostic. Unrelated tools and third-party MCP tools are
not routed. Failures are logged distinctly and fail open; this is not a
security boundary. Native or organization-level model substitutions may still
require investigation of actual execution.

## Workflows and diagnostics

| Skill | Purpose |
|---|---|
| install | Configure this harness's native artifacts; preview/check/uninstall/rollback. |
| doctor | Check installation, pricing, model references, and runtime evidence without paid calls. |
| tune-routing | Configure tiers and diagnose actual native model selection; opt into Claude Code's advisor. |
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

Default policy bodies are about 2.5 KB, with a 2.6 KB component budget.
Measurement counts metadata separately and treats bytes/4 only as a rough
prose-token proxy. Harness wrappers, history, tools, cache behavior, and child
work are outside that static measurement. No assertion is made that instruction
overhead always pays for itself. `claude plugin details leos-agent` lists hooks
as having no model context cost, so its estimate leaves out the policy the
SessionStart hook injects; `measure_context.py` counts it. On Claude Code,
`/skill-doctor` reports what each skill's listing costs and how often it runs.

Usage scanning handles Claude streaming duplicates, Codex cumulative/cache
accounting, and OpenCode message-time usage. Cursor/Hermes/Pi usage schemas are
currently unsupported and reported that way. Reference prices are not bills;
unknown cache/model costs remain explicit. Compaction pre-context counts are
not discarded tokens. Requested/corrected/blocked dispatches are distinct from
observed child models. Missing/rotated logs do not prove a broken install.

PR review pins the full head SHA and reads code only at it, from GitHub, never
from a local checkout; it never silently moves line anchors or retries old
findings against a new head. Replacing a pending review requires
an unchanged ownership receipt and saves recovery data. New comments/replies
remain pending. Only verified addressed threads rooted by the authenticated
user can be auto-resolved; resolution is public and requires SHA-bound evidence.
Every changed non-generated file is read before staging; after a complete
review whose draft is no longer pending, a new head re-reads only the files
whose patch changed. Each finding is
re-checked at the SHA before staging, and the repository's own CLAUDE.md,
AGENTS.md and REVIEW.md rules count only when quoted. Review runs at the
standard tier; a consequential PR or an escalation runs it at premium. Neutral
and seriously-problematic always come with a pending comment; CI never affects
the verdict. A ready-to-merge verdict stands until the PR diff changes, hunk
line numbers aside: a merge from the base keeps it unless the base touched
lines within three of a PR hunk or a file GitHub sends no patch for. An
optional cross-model lens, off unless the user enables it, sends the pinned
diff to a second provider's CLI.
Watchers use cross-process leases, bounded retries, and completion reports;
emission alone never records a PR as reviewed. A head waiting on the user's
decision is parked without spending retries.

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

`.githooks/pre-commit` runs the same four. Git ignores it until a clone opts
in with `git config core.hooksPath .githooks`, and it checks the working tree,
unstaged edits included, not just what is staged.

Tests use fixtures and mock provider events; CI makes no paid model calls.
Native live smoke tests are separate, explicitly authorized, and budgeted.
Static/adapter tests establish contracts, not complete end-to-end parity on
all six installed harnesses.

Local Claude smoke tests on 2026-09-08 verified actual Haiku/Sonnet children
from an Opus parent and a Sonnet-to-Haiku correction when the Haiku parent was
observable. Total CLI-reported reference cost, including diagnostic runs, was
about $0.223. A fresh session's first dispatch can precede parent transcript
persistence, so that case remains an explicit unknown-parent allowance.

Commits run validation only; releasing is the workflow's job. Every push to
`main` starts the release workflow, which bumps the version, validates the
bumped tree, and pushes the release commit and its tag in a single atomic push
before publishing to npm. Release versions use `12.YYYYMMDDXX.0`, with a UTC
calendar date and a two-digit serial from 00 to 99.

That push is a compare-and-swap, so a run whose `main` moved underneath it
pushes nothing and defers: the commit that beat it has a run of its own, and
that run releases a tip already containing both. Pushing a `v*` tag by hand
still publishes through the same workflow, so `scripts/bump.py` remains the way
to prepare a release outside CI. npm publishing uses its existing
trusted-publishing workflow, publishes a given version at most once, and only
moves `latest` forward: an older tag re-run after a newer release publishes
under the `backfill` dist-tag. An ordinary code commit never automatically
changes versions or stages unrelated files.

Native references: [Claude subagents](https://code.claude.com/docs/en/sub-agents),
[Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents),
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Cursor plugins](https://cursor.com/docs/reference/plugins),
[OpenCode plugins](https://opencode.ai/docs/plugins/),
[Hermes hooks](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/hooks.md),
[Pi extensions](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md).
