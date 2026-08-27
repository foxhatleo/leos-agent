---
description: Resume work from a leos-agent handoff document written by an earlier session.
argument-hint: "[name]"
---

Use the `handon` skill on `$ARGUMENTS`.

With no argument or an ambiguous one, list the handoffs and ask which — never
pick one unprompted. Read it, compare its frontmatter against the current
directory, branch, and HEAD, and report any drift before anything else.
