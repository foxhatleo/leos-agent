---
description: An independent, output-heavy retrieval step. With the plugin the parent should hand the bulk reading to the cheap tier and still return the right answer.
tags: [routing]
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Agent]
---

Our on-call write-ups from the last few months are the Markdown files in the read-only `incidents` directory in your working directory. I need every incident that was resolved by rolling the change back and whose root cause was a configuration change rather than a code change. Read each write-up's root cause and resolution; some mention a rollback that was discussed but not done.

End your reply with exactly one plain-text line: `ANSWER: ` followed by the matching incident IDs in ascending order, separated by a comma and a space.
