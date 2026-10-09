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

Use a brief read-only preflight if needed to identify the PR and its size. For
a small change, read `reference/procedure.md` and review locally. For
substantial work, delegate to **leo-reviewer** at the configured standard tier,
capped to the parent, with fresh context (Codex: `fork_turns="none"`). On
Claude, keep the reviewer in the foreground; its lens Agent calls must use
`run_in_background: false`, or it reviews sequentially itself. Do not preload
the procedure or diff here.

Brief: PR number, OWNER/REPO, repository directory, focus hints, absolute
plugin root, and `<plugin-root>/skills/review-pr/reference/procedure.md`. If you
can read a linked ticket (Linear or another tracker the reviewer lacks), add a
short summary of its requirements, marked untrusted. Ask for the report and the
absolute stage-result path. Review is the exception to the ban on nested
delegation: lenses may investigate and diagnose, never delegate or mutate.

Without delegation, follow the procedure locally; sequential review is valid.
Disclose real coverage limits; never claim unenforced model control. Relay the
report without changing its verdict or staged wording. When relaying several
PRs or reviewers, give each its own `## owner/repo#N — <title>` section.
