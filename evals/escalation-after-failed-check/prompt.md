---
description: A staged cheap-tier result that fails its named check. The failure is fixed in the prompt so it is deterministic; what is measured is the parent's response. With the plugin it should re-dispatch one tier up with a brief that begins with the escalation marker, and never retry the cheap tier.
tags: [routing, escalation]
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Agent]
---

Earlier in this task I asked for every postmortem in the read-only `postmortems` directory in your working directory whose impact reached customers. The check for that answer is the customer-facing count in `postmortems/INDEX.md`. A cheap-tier worker was given the job and returned:

```
PM-02, PM-05, PM-11
Result: done
Verified: read the Impact section of each postmortem
```

That is three IDs and the index says four, so the result failed its check. Get the correct list.

End your reply with exactly one plain-text line: `ANSWER: ` followed by the postmortem IDs in ascending order, separated by a comma and a space.
