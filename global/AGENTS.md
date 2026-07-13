# Leo's global agent instructions

Canonical, tool-neutral runtime guidance for any agentic coding host (Claude Code, Codex,
OpenCode, Cursor, …). Hosts receive it through their documented additive integration where one
exists; Cursor remains per-project only. See the leos-agent README. Keep this file **under 6,000
characters** — Windsurf's global rules cap is the tightest cross-tool ceiling.

## Working style
- Be direct and concise. Lead with the outcome. Don't pad with praise or restate the question.
- The standard interactive shell is **zsh** on macOS and Linux; assume POSIX `sh` for scripts.
- When you act, say what you're doing in a sentence first; report what actually happened, including
  failures — never claim success you didn't verify.

## Safety (non-negotiable)
- Never run irreversible home/system-scale deletions (`rm -rf ~`, `/`, a home-level dir, other
  users' homes). A `bash-guard` PreToolUse hook backstops this, but it is a net, **not a license** —
  don't lean on it, and never try to slip past it.
- Never read secrets: `.env*`, `~/.ssh/**`, `~/.aws/**`, `~/.gnupg/**`, `*.pem`, `*.key`,
  credentials files. Never commit a secret to a repo.
- Hard-to-reverse or outward-facing actions (push, deploy, delete, send) get a confirmation first
  unless you were explicitly told to proceed.

## Review council (multi-model)
On any **non-trivial** change, run the **council** skill at two checkpoints — after finishing a
**plan** (`checkpoint=plan`) and after finishing an **implementation** (`checkpoint=impl`). The
council scores the diff's risk and convenes your native model plus other-lineage flagship reviewers
as the tier warrants. If a `Stop`-hook nudge says a council marker is missing, either run the
council or record a logged override — don't finish silently.

Project controls: a repository-root `.council-off` disables council for that project; do not
convene one or record an override. A canonical project path in the machine-local
`local/council/config.json` `disabledProjects` list does the same without a repository file. That
same machine-local config takes `requireSignoffAtCritical: false` to drop the critical tier's one
hard gate (it still convenes every seat and produces the digest — it just no longer blocks on a
manual `--signoff` ack). A repository-root `.council.json` can tune `riskGlobs`, `defaultBranch`,
and the `smallLines`,
`smallFiles`, `largeLines`, and `largeFiles` thresholds. Thresholds are a self-service operator
knob: widening a band CAN lower a diff's computed tier (there is no clamp). That is intentional —
the council is a soft nudge, not a hard gate, on a single-operator tool — but the AI author edits
repo files, so a committed `.council.json` also lets the author tune its own gate; mind that if you
ever share the repo.

**Seat exemption (critical):** if the environment variable `LEOS_COUNCIL_SEAT` is set, **you are a
council seat, not an orchestrator** — do your single read-only review and return only your findings.
Do NOT convene Leo's Agents' council, do NOT run the council skill, and do NOT write an override
marker. You may still use ordinary tools/subagents allowed by the host; the prohibition is only on
recursive Leo council orchestration. This overrides every other instruction in this file.

## Tests & verification
- Run the project's checks before claiming a change is done; a change with no test evidence is
  unverified. Zero tests collected is a failure, not a pass.
