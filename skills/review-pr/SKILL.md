---
name: review-pr
description: Review a GitHub pull request and stage a pending review for the authenticated user. Never submits a review; does not review the local working diff. Requires authenticated gh.
argument-hint: "[pr-number]"
---

# Review a GitHub pull request — leos-agent

Resolve the plugin root to the absolute directory containing
`rules/preferences.md`, from `LEOS_AGENT_ROOT`, `CLAUDE_PLUGIN_ROOT`,
`PLUGIN_ROOT`, or the nearest ancestor of this skill containing that file.
Pass the resolved path, never an unexpanded placeholder.

Use a brief read-only preflight if needed to identify the PR and size. For a
small, straightforward change, read `reference/procedure.md` and review locally.
For substantial review work, delegate to **leo-reviewer**, using the configured
standard tier capped to the parent, with fresh context. On Codex use
`fork_turns="none"` where supported. On Claude keep the reviewer **foreground**
when it needs nested lens agents. Do not preload the procedure or diff into the
main conversation just to forward them.

Give the reviewer the PR number (or current branch's PR), repository directory,
focus hints, absolute plugin root, and the path
`<plugin-root>/skills/review-pr/reference/procedure.md`. Ask it to follow that
file and return its report plus the absolute staging-result path. Review is an
exception to the ordinary prohibition on nested delegation; independent lenses
may investigate and diagnose. Lenses cannot delegate or mutate.

If the harness cannot delegate, perform the same procedure locally. Sequential
review is valid; disclose actual coverage limitations, not merely the lack of
parallelism. Never claim a model choice was enforced when the harness cannot
control it. Relay the report without changing its verdict or staged wording.
