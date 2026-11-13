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

## Quickstart

Install only the harness you use; [Install and upgrade](#install-and-upgrade)
covers upgrades, prerequisites, and per-harness limits.

| Harness | Install | Then |
|---|---|---|
| Claude Code | `claude plugin marketplace add foxhatleo/leos-agent`<br>`claude plugin install leos-agent@leos-agent --scope user` | Start a new session |
| Codex | `codex plugin marketplace add foxhatleo/leos-agent`<br>`codex plugin add leos-agent@leos-agent` | Run `$leos-agent:install`, then review hooks in `/hooks` |
| Cursor | `git clone https://github.com/foxhatleo/leos-agent ~/.cursor/plugins/local/leos-agent`<br>`python3 ~/.cursor/plugins/local/leos-agent/scripts/leo-install.py cursor` | Reload the plugin |
| OpenCode | `git clone https://github.com/foxhatleo/leos-agent ~/.local/share/leos-agent`<br>`python3 ~/.local/share/leos-agent/scripts/leo-install.py opencode` | Restart OpenCode |
| Hermes | `hermes plugins install foxhatleo/leos-agent --enable` | Start a new session |
| Pi | `pi install git:github.com/foxhatleo/leos-agent` | Start a new session; delegation needs a subagent extension |

## Routing and cost

| Tier | Appropriate work | Claude default | Codex default |
|---|---|---|---|
| Cheap | Bounded factual checks, mechanical work, known procedures | Haiku | GPT-6 Luna |
| Standard | Investigation, implementation, diagnosis, ordinary review | Sonnet | GPT-6.1 Sol |
| Premium | Difficult diagnosis, cross-module design, complex implementation, consequential review | Opus | GPT-6 Astra |
| Parent-level | Exceptional work beyond premium capability that justifies delegation | Current parent | Current parent |

Native profiles are `leo-cheap`, `leo-standard`, `leo-premium`, `leo-parent`,
`leo-reviewer`, and `leo-lens`. The retired `leo-runner` and `leo-executor`
profiles are gone, but the guard and logs still map those names to cheap and
standard, and routing config still accepts its `runner` and `executor` keys.
Worker profiles cap their turns on Claude Code, where a capped run returns its
output marked partial, and on OpenCode as `steps`, past which the worker loses
its tools and answers in text. The Codex and Cursor profiles carry no turn cap.
Review may use nested lenses through `leo-lens`, the read-only lens profile.
Claude Code removes its Edit, Write and Agent tools, and the reviewer sets its
cheap or standard tier with the dispatch's `model`. OpenCode denies it `edit`
and `task` and Cursor marks it `readonly`; there and on Codex it is a
standard-tier profile, and a cheap lens uses `leo-cheap`. A Codex lens runs in
the session's own sandbox, since a Codex role cannot narrow it, so it stays
read-only there by instruction. The price guard never moves a lens to another
profile: where it cannot hold a lens within the reviewer's price, it refuses
the lens and the reviewer reviews that area itself. Bash stays available for
reading the PR. Ordinary workers do not delegate. A small review outside a
watch may run locally; a large or risky one gives each named lens its own
files rather than having every lens reread everything.

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
under a GPT-6.1 Sol parent is replaced with the parent where supported. A child
counts as within the ceiling only when its cache read and write rates are no
dearer either, since a child mostly re-reads cached context. Crossover
rates, unknown IDs, and ambiguous catalog matches are allowed with diagnostics,
as configured by this project's policy. Thus the ceiling prevents **known**
overselection, not every possible billing outcome.

Prices come from the public [OpenRouter model catalog](https://openrouter.ai/docs/guides/overview/models),
including Claude, GPT, DeepSeek, Kimi, GLM, and Qwen families. Exact IDs and
recognized aliases are preferred. Nearby versions of the same variant can use
an explicitly labeled estimate; sizes, cheap/pro variants, and free endpoints
are not conflated. Claude's Amazon Bedrock and Google Cloud Agent Platform
(Vertex AI) IDs take the first-party model's price, so the ceiling also holds
under those parents. A price alias never changes the identifier sent to a harness.
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
| Claude Code | SessionStart, including forks; the Claude render leaves out lines Claude Code's own Agent guidance already covers | `agent.spawn` model setting on builds where it covers teammates too ([hooks/README.md](hooks/README.md#claude-agentspawn-mod)), else Agent/Task argument correction; native profiles | SubagentStop final message, child transcript fallback, child usage from transcript | On the Agent/Task path the first dispatch may precede parent transcript persistence; missing parent data permits dispatch with a diagnostic. Forced settings/provider substitutions also limit enforcement. |
| Codex | Separate native SessionStart hook | Tier-enforcing explicit spawn selection; model-free native profiles | SubagentStop final message, rollout fallback, cumulative token counts | Hooks need native trust, renewed in `/hooks` after a hook or a script it runs changes. A renamed multi-agent tool namespace is not matched. Other/customized profiles can still override spawn settings. Encrypted briefs make the escalation marker unobservable; recorded as such. |
| Cursor | Native always-apply rule, plus the live rendered policy as sessionStart `additional_context` | Installed user agents; resolved subagentStart model ceiling | subagentStop summary and status, joined by call id where Cursor sends one; a worker missing its contract lines is asked once through `followup_message`; no usage | No invented Task model argument; hook diagnostics distinguish planned models from completion. Worker no-delegation is instruction-only: no per-agent tool restriction, and no parent-agent identity at subagentStart. |
| OpenCode 1 (1.4+) | One registered rendered instruction | Native agent selection, confirmed through the SDK | `tool.execute.after` output of a foreground `task`, joined by call id, child model from task metadata; no usage | Task has no model field; a correction that cannot be applied blocks the task. A slash-command subtask's own model is not visible to the guard. Source/config paths must remain valid. |
| OpenCode 2 | Plugin context hook, rendered once per session | `subagent` model argument, moved to the parent's model when the call or its agent would run over it; native agents listed through the plugin context | `execute.after` result of a foreground `subagent`, joined by call id, child model from the child session's own requests; no usage | `instructions` entries are not loaded. A background subagent's completion is not observed. Source/config paths must remain valid. |
| Hermes | Frozen system-prompt section | Global native delegation-model ceiling when parent/model are observable | `subagent_stop` per child (`post_tool_call` only when the build lacks it); no usage | Native delegation has one global model, not separate per-task tiers. Older builds skip post-tool hooks for built-in tools; the report then says no completion signal was observed. |
| Pi | Extension caches rendered body per session | Advisory policy; dispatches are logged without a model check | `tool_result` text and usage for a tool named `subagent` | No native per-spawn model guarantee for third-party subagent tools. |

Completion capture reads the last 4 KiB of a worker's final text, or of the
report a Claude child handed back through its hand-back tool, for its
`Result:` and `Verified:` lines and drops the text. On Claude Code, Codex and
Cursor, a leo-* worker whose final message lacks those lines is asked once, at
its first SubagentStop, to restate its report with them. In Claude Code auto mode, where
a worker reports through the hand-back tool instead, its first report without
them is refused once and the worker is asked to hand it back with both lines.
Guard modes `warn` and `off` skip both. The dispatch log stores the outcome enum, a verified
tri-state, a source token, token and turn counts, the tier, the escalation
source tier, and on Claude whether a leo-* worker's hand-back was refused; never
brief or result text. A Claude `leo-lens` is logged under the tier of the
model it ran on: cheap on the configured cheap model, otherwise standard.
`dispatch_log.py report` joins
completions to dispatches by call id; on Claude, whose SubagentStop has none,
through a PostToolUse row linking each Agent call to the child it started;
and only without either, by the nearest preceding same-session dispatch of
the same tier. The report prints
outcome and verification counts per tier beside the dispatches that sent no
completion signal and the children whose hand-back was refused, escalation chains and tier counts over dispatches that ran,
summed child usage and turns, reference cost per verified success from catalog
prices (an estimate, not a bill), and which harnesses supplied no signal. It
counts and prices a child's tokens by the usage scan's own rules.
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

Requires macOS or Linux, Python 3.9+, and a current public harness. Claude
Code builds up to the one [hooks/README.md](hooks/README.md#claude-agentspawn-mod)
names reject the plugin's hooks file, so none of its hooks load there. GitHub
workflows need authenticated `gh`. JavaScript harnesses supply their own Node/Bun runtime.
No OpenAI or Anthropic marketplace submission is needed: use this repository
as your own plugin source or a local checkout.

Install **only the harness you intend to configure**. Native plugin discovery
and the integration installer are separate steps. The installer writes files
only for Codex, Cursor and OpenCode; on Claude Code, Hermes and Pi it only
removes a `<leos-agent>` block an older release left in the harness's global
instruction file, so on a fresh machine it has nothing to do there. Where it
writes files, run it again after upgrades or model-mapping changes. It needs
the harness's config directory to exist already (run the harness once), and
never creates one. A successful install ends with a `Next steps (<harness>):`
line; on Claude Code there is nothing to install.

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

Optional cost status line, never installed by default:

```sh
python3 "<plugin-root>/scripts/leo-install.py" claude --statusline
```

It sets `statusLine` in Claude's `settings.json` to a small launcher it writes to
`~/.leos-agent-local/bin/statusline` (private, outside the versioned plugin cache).
At run time the launcher picks the plugin root from `LEOS_AGENT_ROOT`, else the
newest `leos-agent` version in Claude's plugin cache, else the root it was
installed from, and runs `scripts/statusline.py` in the same process, so plugin
upgrades never need a reinstall. The script prints one short line from Claude
Code's status-line JSON and the local dispatch log: the model, Claude Code's own
session cost estimate, and this session's children by tier with their reference
cost (the same counting and pricing as `dispatch_log.py report`; a reference
estimate, not a bill). It reads local files only, never the network or a model,
and prints the model name, or just "Claude", on any error. A different
`statusLine` already set is a conflict: `--force-path <path to settings.json>`
replaces it, and `--uninstall` or `--rollback` puts your previous one back and
removes the launcher. `/leos-agent:doctor` reports whether the line is installed
and the launcher resolves a plugin.

### Codex

```sh
codex plugin marketplace add foxhatleo/leos-agent
codex plugin add leos-agent@leos-agent
```

Then run the install skill in a Codex session by mentioning `$leos-agent:install`
(or pick it from `/skills`). It is required here: it writes the cheap,
standard, premium, parent and reviewer agent TOMLs, plus the read-only review
lens, into `~/.codex/agents`.
Review the current hook definitions in `/hooks`; enabling a plugin does not
trust its hooks automatically. On upgrade, refresh the marketplace/plugin,
rerun `$leos-agent:install`, and review `/hooks` again, also when only a
script a hook runs changed. The project does not bypass that native trust
boundary.

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

The plugin's hooks run `scripts/cursor_hook.py` through `$CURSOR_PLUGIN_ROOT`,
since Cursor runs `subagentStop` from the workspace root. `sessionStart` adds
the live rendered policy beside the always-apply rule, `subagentStart` checks
the model ceiling, `subagentStop` re-prompts a worker missing its contract
lines once, and `preCompact` shows a handoff reminder. Every hook fails open.

### OpenCode

A stable checkout avoids versioned package-cache paths:

```sh
git clone https://github.com/foxhatleo/leos-agent ~/.local/share/leos-agent
python3 ~/.local/share/leos-agent/scripts/leo-install.py opencode
```

Then restart OpenCode. The installer registers the checkout's plugin URI and
one rendered instruction in the active config (`OPENCODE_CONFIG`, else
`opencode.jsonc`, else `opencode.json`), and copies the native agent profiles
(the four tiers, the reviewer and the read-only review lens) and, on OpenCode 1,
skill/reference copies with the checkout's absolute path baked in. On OpenCode
2, which the installer recognises from `opencode --version`, the plugin
registers the skills itself: the installer copies none and takes back unchanged
copies an earlier install left, keeping edited ones, which OpenCode then uses
in place of the plugin's. It preserves JSONC comments and
unrelated configuration, and uninstall edits the same file the install did.
Inside OpenCode the install skill is `/leo-install`. Upgrade the checkout with
`git pull --ff-only`, rerun the same command, and restart. The rendered
instruction is a copy of the policy: it changes only when the installer reruns,
and `--check` reports it out of date after an upgrade or a routing change.

The npm package is also published as `leos-agent`. On OpenCode 1:

```sh
opencode plugin leos-agent --global
root="${XDG_CACHE_HOME:-$HOME/.cache}/opencode/packages/leos-agent/node_modules/leos-agent"
python3 "$root/scripts/leo-install.py" opencode
```

On OpenCode 2, `opencode plugin add` installs it under a timestamped directory:

```sh
opencode plugin add leos-agent
root="$(ls -d "${XDG_CACHE_HOME:-$HOME/.cache}"/opencode/npm/leos-agent@latest/*/node_modules/leos-agent | tail -n 1)"
python3 "$root/scripts/leo-install.py" opencode
```

Run from OpenCode's package cache, the installer keeps the package entry
(`plugin` on OpenCode 1, `plugins` on OpenCode 2) rather than linking into the
cache, so OpenCode can fetch the package again after cleaning it. The skill
copies still point into the cache: rerun the installer whenever that directory
changes (each OpenCode 2 package update, or a pinned `leos-agent@<version>`
spec). Installing from a checkout takes a `leos-agent` package entry out so the
plugin does not load twice, and uninstall puts it back.

OpenCode 2 does not load `instructions` entries. There the plugin adds the
same policy to each session's model requests through its context hook,
rendered once per session, so it follows upgrades and routing changes without
a rerun. The native agent profiles load on both. OpenCode 1
needs 1.4 or later, the first release that loads the plugin's single default
export; the doctor skill reports both cases for the `opencode` on `PATH`.

### Hermes

```sh
hermes plugins install foxhatleo/leos-agent --enable
```

This installs into `$HERMES_HOME/plugins/leos-agent` (default
`~/.hermes/plugins/leos-agent`). For a local checkout instead, clone it there and
run `hermes plugins enable leos-agent`. Start a new session. The native plugin
registers portable skills (as `/leos-agent:leo-<name>`), one frozen policy
section, dispatch diagnostics, and the `/leo-install` command; that command
only removes a block an older release left in `SOUL.md`. Hermes has no
user-only skill flag, so it lists the user-invoked skills to the model too.
Hermes supports the policy for deciding whether to delegate, but not per-task
model tiers. The installer preserves its native
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
conflict that stops the run (`--force-path PATH`, repeatable, replaces that
file; `--force` replaces every conflicting file); an edited copy from a
release older than v12 is reported as `preserved` and the rest continues, and
only a `--force-path` naming it replaces it. The older `--force PATH`
spelling still works. `--check` exits 3, not 0, while a preserved copy keeps
an older payload. A pending backup left by an interrupted install blocks the
next install until `--rollback` undoes it. Uninstall and rollback remove only
directories an install created. On uninstall, anything not provably ours is
`preserved`, and `--force` does not change that. Copies of the retired `leo-runner` and `leo-executor` profiles are
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

On Claude Code, `CLAUDE_CODE_SUBAGENT_MODEL` keeps its native role as a default.
A general-purpose or claude dispatch that names no model runs on it instead of
the standard tier, capped at the parent's price; the leo tiers, and Explore and
Plan (defined as `inherit`), keep their own model. With
`CLAUDE_CODE_SUBAGENT_MODEL_FORCE` on, the setting replaces every tier, and the
guard only blocks a forced model priced above the parent.

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
| tune-routing | Configure tiers and diagnose actual native model selection; opt into Claude Code's advisor or `opusplan`. |
| review-usage | Mechanical usage scan with reference-cost estimates and explicit gaps. |
| review-pr | Review a pinned GitHub PR from a read-only worktree, stage it as pending, submit once you agree, and mark it watched. |
| handoff / handon | Save concise context pointers and resume after checking drift. |
| watch-review | Claude Monitor watcher that reviews, re-reviews and answers what is addressed to you like a human reviewer; code polls GitHub, models run only when there is work. |
| attach-pr | Claude-specific attachment of an existing PR to the desktop session. |

Only small routing instructions and necessary metadata are always present.
Detailed review procedures remain in deferred reference files. Duplicate
slash-command wrappers are removed; native skills provide invocation. Explicit
invocation controls do not universally imply hidden metadata; measure both.

On Codex and Cursor, a PreCompact hook shows a one-line warning before each
top-level compaction pointing to `$leos-agent:handoff`; the model never
sees it, and compaction always proceeds. On Codex it needs trust in `/hooks`
like every changed hook. Claude Code discards a PreCompact hook's message, so
it has no equivalent.

```sh
python3 scripts/doctor.py --harness claude --json
python3 scripts/usage_scan.py --since 7d --harness claude --json
python3 scripts/dispatch_log.py report
python3 scripts/measure_context.py --check
python3 scripts/pricing.py resolve claude-sonnet-5
```

Default policy bodies are 2.3 to 2.6 KB, with a 2.6 KB component budget;
Claude's is the smallest because it drops lines Claude Code's own guidance
covers. Every skill body and reference file has its own budget.
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

Logs omit prompt text by default and rotate at 1 MiB plus one retained file.
`LEOS_AGENT_DISPATCH_LOG_PROMPTS=1` is an explicit debug option that retains
brief excerpts; avoid it for sensitive work. Installation backups and handoffs
stay local. Handoff names are validated, reservations avoid collisions, and
symlink escapes are refused by the helper.

### PR review and the watcher

`/review-pr` reviews one PR the way a careful human reviewer would, and
nothing goes public until you answer for that PR.

- **Pinned and read-only.** It pins the full head SHA and checks it out as a
  detached worktree outside your projects, with git hooks off, so reviewers
  can read whole files, callers and tests. Line anchors still come from
  GitHub at that SHA; a moved head means a re-review, never old findings
  moved onto new lines. Reviewers never run the PR's code.
- **Running code only on your yes.** When reading cannot settle something
  (frontend behaviour especially), the report proposes commands under
  `Exercise:` and you can pick "Run checks first". Your session then runs
  them with a rebuilt environment holding no tokens, binding servers to
  localhost. A PR from a fork never runs.
- **Coverage.** Every changed non-generated file is read before staging. A
  name alone exempts only a file the PR modifies, never one it adds, and a
  lockfile edit that adds a package origin is read too. After a complete
  review, a new head re-reads only the files whose patch changed.
- **Lenses.** Security and correctness always; tests, performance,
  frontend/UX and requirements when the PR touches them. Over about 150
  changed lines, or in a risky area (auth, money, migrations, infra,
  secrets), each runs as its own read-only `leo-lens`; otherwise the reviewer
  works through them as a checklist. A blocking finding is confirmed by a
  fresh-context lens at the standard tier or above before it is staged.
- **Others' input.** Bot reviews (Copilot, Cursor, Claude and others), tool
  reports such as Sonar, failed check annotations and other people's comments
  are candidates to verify. One that holds is restated in your voice as your
  own comment, never as a reply on their thread.
- **CI.** Pending checks never delay a verdict. A failure the PR caused is a
  finding; a flaky or unrelated one is a note to you, never posted.
- **Verdict.** Data loss, a security hole or a crash on a realistic path
  requests changes; something that does not work as intended comments; minor
  findings, nits and concrete test gaps ride on an approval. Every review
  carries a body, and an approval is one specific line, never "LGTM". A
  ready-to-merge verdict stands until the PR diff changes, hunk line numbers
  aside. The repository's own CLAUDE.md, AGENTS.md and REVIEW.md rules count
  only when quoted.
- **Writing.** Short, casual, specific, `nit:` for nits, tables when they
  help. Text with an em dash is refused before it reaches GitHub.
- **Replies.** It answers only comments addressed to you after your last
  one, in threads you started, replied in or were @mentioned in, and in the
  PR conversation when someone answers your review or comment. It resolves
  only threads you started, with SHA-bound evidence of the fix, and rebuts a
  thread at most once.
- **Drafts.** Comments and replies are staged pending. Replacing a pending
  review needs an unchanged ownership receipt and saves recovery data; a
  draft you edited is yours to submit.

Each PR you are asked about opens with a header and its round:

```
## Alice Chen [ABC-123] Fix pagination for empty pages
Round #2: New commit
```

The round counts every pass on that PR. Its label is `Initial review`,
`New commit`, `Comment response`, `New commit + Comment response`, or
`New commit (base merge only)`. Answers can come late: before acting, the
review rechecks the PR, applies what is still valid, re-reviews only what
drifted, and tells you in one line what changed meanwhile.

When you submit a review it staged, the next review of that PR notes which
staged comments you deleted, rewrote or downgraded, as data-only entries in
`~/.leos-agent-local/review-learnings/<owner>__<repo>.json`. A learning may
omit a minor or nit finding or downgrade a major one, never omit a major one
or touch a blocking one. `ghreview.py learnings -R OWNER/REPO list` shows
them and `forget --id ID` or `forget --all` drops them. Review runs at the
standard tier; a consequential PR or an escalation runs it at premium. An
optional cross-model lens, off unless you enable it, sends the pinned diff to
a second provider's CLI.

`/watch-review` (Claude Code) follows a repository from one session:

| Source | What happens |
|---|---|
| A PR you ran `/review-pr` on | Watched at once |
| A direct review request (not a team request) | Reviewed |
| A re-request at a head already reviewed | Looked at again |
| A new head on a followed PR | Re-reviewed |
| A reply or comment addressed to you | Judged against the code and answered |
| Your approval gone stale where the repo wants a fresh one | Offered a one-line re-approval |

A PR stays followed until merged or closed; there is no unwatch, and a draft
pauses it. Up to two background reviewers work while you are asked about one
PR at a time in a blocking question. A head waiting on a decision only you
can make is parked without spending retries. Approval freshness is read from
the base branch's rules (`watch_review.py config --freshness auto|strict|off`
overrides it). Each piece of work is one line:

```
<verb> OWNER/REPO#N head=SHA [prev=SHA] [threads=K] [conversation=1] [by=LOGIN] claim=TOKEN URL | TITLE
```

The verbs are `review-requested`, `re-review`, `re-requested`, `follow-up`
and `re-approve`, and everything after the first ` | ` is the untrusted
title. Watchers use cross-process leases, bounded retries and completion
reports; emitting a line never records a PR as reviewed.

### Evaluating cost and quality

`evals/` is a [`claude plugin eval`](https://code.claude.com/docs/en/plugin-evals)
suite for the delegation policy on Claude Code. Each case runs three times with
the plugin and three times without it, every run in a fresh temporary home, so
the plugin's default tiers apply rather than your `routing.json`. A run receives
only `EVAL_*` variables beside an allowlist of your shell, so each case sets
`EVAL_LEOS_AGENT_PRICE_REFRESH=off`, which the SessionStart hook honours like
`LEOS_AGENT_PRICE_REFRESH=off`; no run starts a price refresh. Fixtures are
small directories inside each case, except `large-corpus-audit`, whose corpus
a seeded `generate.py` writes at staging. A run starts in an empty workspace and
`add_dirs` would only grant reads to a path the model is never told, so each
case's `stage-fixtures.sh` copies its fixtures into the workspace read-only.
Pass `--scaffold` so it runs; without it every case scores 0 in both arms. No
case needs `--allow-tools`, and every grader is a regex or tool-use check, so
no judge model is called.

| Case | Expected with the plugin |
|---|---|
| `cheap-retrieval` | Correct answer. The read stays local: the write-ups carry traps, so checking a cheap worker's list means rereading them, and the policy keeps a step local when nothing cheaper checks it. Its cheap-delegation indicator reports any change |
| `local-small-task` | No delegation in either arm; correct answer |
| `worker-contract` | The worker's reply carries `Result:` and `Verified:` lines; correct answer |
| `escalation-after-failed-check` | A staged cheap result fails its check; re-dispatch to standard with an `Escalation from cheap:` brief, no cheap retry; correct answer |
| `quality-guard` | Correctness only |
| `bulk-triage` | Correct answer. Thirty-six prose tickets with an index count as the check, so the policy allows a cheap worker to do the read. Its cheap-delegation indicator reports whether one did |
| `separable-audit` | Correct answer. Two independent scans, each with its own index count, that could go to separate cheap workers. Same indicator |
| `large-corpus-audit` | Correct answer. About 400 KB of incident write-ups with traps and an index count, where a cheap worker doing the read should pay off. Same indicator |

```sh
claude plugin eval . --scaffold --no-publish --max-cost-usd 8
claude plugin eval . --scaffold --case local-small-task --runs 1 --ablation none
```

Every run is a real model call billed to your plan or API account. A full run
is 8 cases × 3 runs × 2 arms = 48 agent runs; `--max-cost-usd` caps the
spend. Each case pins its parent in `model:`: Opus for `cheap-retrieval`,
`escalation-after-failed-check`, `bulk-triage`, `separable-audit`, and
`large-corpus-audit`, whose
routing needs tiers below the parent
(under Sonnet, one tier up from cheap is the parent's own price, which the
policy keeps local), and Sonnet for the rest. `--model` overrides every case's
pin, so pass it only to compare parents. Runs inherit most `CLAUDE_CODE_*`
settings from your shell, so unset overrides such as
`CLAUDE_CODE_SUBAGENT_MODEL` first.

`WITH` and `W/OUT` count only the graders scored in both arms: each case's
correct answer, plus the no-delegation check on `local-small-task`. `Δ` is
therefore a quality difference on that task (and, there, over-delegation), so a
negative `Δ` is a regression. With three runs per arm, one run moves a
single-grader case by 0.33. The routing, contract, and escalation graders are
with-arm indicators, because the baseline has no `leo-*` agents;
`--ablation none` scores them, so use it to gate on behaviour (`cheap-retrieval`'s
indicator is expected off). At the default threshold of 1.0, one failed scored
grader fails the case. `COST` is the CLI's list-price estimate for both arms of
a case together. It is not a bill, and the summary does not split it by arm.
Results show how the policy behaves on these eight tasks with the pinned model;
they do not establish savings in general.

`scripts/bench.py` turns the suite into a paired cost benchmark. `run` calls
`claude plugin eval` with both arms, `--scaffold`, and `--keep-temp`, reads
each run's own usage record (list-price cost, turns, and per-model new-input,
cache-read, and output tokens, children included), then deletes the kept run
directories. It pairs run *i* of the with-plugin arm with run *i* of the
baseline in the same case and tests the paired differences with an exact
Wilcoxon signed-rank test, reporting the Hodges-Lehmann estimate with its
confidence interval and the rank-biserial effect size. Correctness is compared
with an exact sign test on the discordant pairs. A run with an error drops its
pair from the metric tests. Every directional statement in the report states
its p and n. Below six nonzero pairs no difference can reach p < 0.05, so the
report makes no cost claim there. `report` re-reads earlier output for free and
pools several runs.

```sh
python3 scripts/bench.py run --runs 3 --max-cost-usd 8 --ledger ~/bench-ledger.txt --budget-usd 15
python3 scripts/bench.py report evals/results/bench-<time> [more ...]
```

`--max-cost-usd` is the CLI's own ceiling, checked before each agent run
starts, so a run can overshoot it by the runs already in flight. With
`--ledger` and `--budget-usd`, `run` refuses to start when the ledger's total
plus the ceiling plus an allowance for in-flight runs would pass the budget,
and appends the run's cost to the ledger. Output goes under `evals/results/`,
which git ignores.

`evals-write/` is a separate suite for cases that change files, kept apart so
the suite above stays read-only and needs no grant. Its one case,
`separable-change`, pins Opus and asks for two independent changes to a small
Python package: a mechanical rename across six modules and a new function with
a unit test of its own. A read-only acceptance test defines the result. The
graders need the acceptance tests and the run's own test passing in a real
`python3 -m unittest` run in the trace, the run's test file, and the
acceptance file left intact. All are scored in both arms. Runs need the
`--eval-dir` and the narrowest grant the case passes with; `bench.py run
--eval-dir evals-write` passes both:

```sh
claude plugin eval . --eval-dir evals-write --scaffold --no-publish --max-cost-usd 3 \
  --allow-tools Edit Write "Bash(python3 -m unittest:*)"
```

Every other shell command, `sed` included, is refused, so a run makes its
edits with Edit and Write. Results go under `evals-write/results/`, which git
ignores. `tests/test_evals_write.py` is its offline gate: it stages the case,
applies a reference change, runs the case's check, and grades that output.

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

On Claude Code the guard reads the parent's model from its transcript, falling
back to the model recorded at session start. A fresh session's first dispatch
can precede both; the guard then treats the parent as unknown and allows the
dispatch without a price check.

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
