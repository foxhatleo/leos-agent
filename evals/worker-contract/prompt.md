---
description: Delegation is requested outright so the worker's reply format can be checked. The plugin's workers close with a Result line and a Verified line.
model: sonnet
tags: [contract]
max_turns: 10
timeout_seconds: 240
allowed_tools: [Read, Glob, Grep, Agent]
---

Each file in the read-only `manifests` directory in your working directory describes one service. Hand the scan to a subagent, since it is a bounded, mechanical read: it should find every service whose manifest has no `owner` field. A commented-out line is not a field. Check what the subagent returns against the files yourself before you answer.

End your reply with exactly one plain-text line: `ANSWER: ` followed by the service names in alphabetical order, separated by a comma and a space.
