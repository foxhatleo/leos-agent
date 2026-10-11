---
description: Two independent bounded scans over two directories, each with its own count check, so each can go to a cheap worker. Benchmarks cost against the baseline; scored on the right answer.
model: opus
tags: [benchmark, routing]
max_turns: 16
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Agent]
---

Two independent cleanup questions; today is 2026-10-01.

1. The read-only `flags` directory in your working directory has one write-up per feature flag. Which flags are past their removal date but still on in production? Being on only in staging does not count. `flags/INDEX.md` gives the count.
2. The read-only `runbooks` directory in your working directory has one runbook per service. Which runbooks cover a tier 1 service and were last reviewed before 2026? `runbooks/INDEX.md` gives the count.

End your reply with exactly one plain-text line: `ANSWER: ` followed by the flag names in alphabetical order and then the runbook IDs in ascending order, all separated by a comma and a space.
