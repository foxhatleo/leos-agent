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
substantial work, delegate to **leo-reviewer** with fresh context (Codex:
`fork_turns="none"`), capped to the parent: standard tier, premium for a
consequential PR (auth, security, data migration, money, or the user says so)
or after `Result: escalate`. Wait for its result. Do not preload the procedure
or diff here.

Brief: PR number, OWNER/REPO, repository directory, focus hints, absolute
plugin root, `<plugin-root>/skills/review-pr/reference/procedure.md`, and any
head SHA, previous reviewed head and claim token a watcher supplied. If you can
read a linked ticket the reviewer lacks, add a short summary of its
requirements, marked untrusted. Ask for the report and the absolute
stage-result path. Lenses may investigate, never delegate or mutate.

The cross-model lens is off unless the user enables it, for this review or
with `review_peer.py config --enable`. When they ask for it here, tell them the
PR diff goes to a second provider, then brief `cross-model: on`.

Without delegation, follow the procedure locally; sequential review is valid.
Disclose real coverage limits; never claim unenforced model control. Relay the
report without changing its verdict or staged wording. When relaying several
PRs or reviewers, give each its own `## owner/repo#N — <title>` section.
