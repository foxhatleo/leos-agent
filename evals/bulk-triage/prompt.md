---
description: Bulk reading of thirty-six prose tickets with a cheap named check (the index count), so the policy lets a cheap worker do the read. Benchmarks cost against the baseline; scored on the right answer.
model: opus
tags: [benchmark, routing]
max_turns: 16
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Agent]
---

This quarter's billing support tickets are the Markdown files in the read-only `tickets` directory in your working directory, one per ticket. I need every ticket where we refunded the customer and the cause was a defect in our billing system, not something the customer did. Read each ticket's Investigation and Outcome sections; some mention a refund that was offered or discussed but never paid, and some suspected a defect that turned out not to be one. `tickets/INDEX.md` gives this quarter's count of refunded billing-defect tickets, which you can use as a check.

End your reply with exactly one plain-text line: `ANSWER: ` followed by the matching ticket IDs in ascending order, separated by a comma and a space.
