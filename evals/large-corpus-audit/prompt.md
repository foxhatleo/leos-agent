---
description: Bulk reading of a corpus of about 400 KB, one hundred and twenty incident write-ups, with a cheap named check (the index count), where handing the read to a cheap worker should pay off at scale. Benchmarks cost against the baseline; scored on the right answer.
model: opus
tags: [benchmark, routing]
max_turns: 24
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Agent]
---

This year's incident write-ups are the Markdown files in the read-only `corpus` directory in your working directory, one per incident. I need every incident where the confirmed root cause was an expired TLS certificate and customers were affected by the outage. Read each write-up's Investigation, Root cause and Customer impact sections; some mention a certificate that was suspected or checked and ruled out, and some confirmed certificate expiries were caught before they reached customers. `corpus/INDEX.md` gives the count of customer-facing outages caused by an expired TLS certificate, which you can use as a check.

End your reply with exactly one plain-text line: `ANSWER: ` followed by the matching incident IDs in ascending order, separated by a comma and a space.
