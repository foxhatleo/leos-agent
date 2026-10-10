---
description: A small dependent lookup. The second read depends on the first, so the work should stay local, with no delegation in either arm.
model: sonnet
tags: [routing]
max_turns: 6
timeout_seconds: 120
allowed_tools: [Read, Glob, Grep, Agent]
---

In the read-only `deploy` directory in your working directory, the `ACTIVE` file names the active profile. Which port does the active profile listen on?

End your reply with exactly one plain-text line: `ANSWER: <port>`.
