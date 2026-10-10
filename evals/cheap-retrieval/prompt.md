---
description: An independent retrieval over short write-ups with traps. Checking a cheap worker's list would mean rereading them, so the policy's verifiability gate keeps the read local; the cheap-delegation indicator reports whether that changes. Scored on the right answer.
model: opus
tags: [routing]
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Agent]
---

Our on-call write-ups from the last few months are the Markdown files in the read-only `incidents` directory in your working directory. I need every incident that was resolved by rolling the change back and whose root cause was a configuration change rather than a code change. Read each write-up's root cause and resolution; some mention a rollback that was discussed but not done.

End your reply with exactly one plain-text line: `ANSWER: ` followed by the matching incident IDs in ascending order, separated by a comma and a space.
