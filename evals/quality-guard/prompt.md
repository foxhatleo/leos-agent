---
description: Correctness only. Its one grader is scored in both arms, so a negative delta here means the plugin made the answer worse on this task.
model: sonnet
tags: [quality]
max_turns: 10
timeout_seconds: 240
allowed_tools: [Read, Glob, Grep, Agent]
---

Each file in the read-only `services` directory in your working directory sets a request timeout and a retry count. A service's worst-case budget is `timeout_ms * (retries + 1)`. Which services have a worst-case budget over 10000 ms? Ignore commented-out lines.

End your reply with exactly one plain-text line: `ANSWER: ` followed by the service names in alphabetical order, separated by a comma and a space.
