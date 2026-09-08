---
name: doctor
disable-model-invocation: true
description: Read-only diagnosis of this harness's installation, routing capability, model-price matches, hook activation, and prompt overhead. Does not launch live model tests.
---

# Diagnose leos-agent

Inspect only the harness the user named or the one currently running. Do not
infer the harness from repository configuration files or install other ones.
Resolve the absolute plugin root from `LEOS_AGENT_ROOT`, `CLAUDE_PLUGIN_ROOT`,
`PLUGIN_ROOT`, or the nearest ancestor containing `rules/preferences.md`.

```
python3 "<plugin-root>/scripts/doctor.py" --harness <harness> --json
```

The helper checks installed artifacts, policy rendering, tier references, and
price freshness. These are disk checks, **not proof of runtime activation**.
A named model can still be unavailable to the user's provider or organization.
A configured native agent can be shadowed by a higher-priority project agent.

Verify runtime evidence available in this session:

- Claude: enabled plugin and SessionStart output; native hook diagnostics;
  any forced model setting. Child transcript observations establish actual
  response models where available.
- Codex: enabled plugin, its separate hooks-codex.json, and trusted current
  definitions in `/hooks`. Native profile model precedence overrides spawn
  choices; do not confuse successful rendering with hook execution.
- Cursor: native rule and installed user agents; Hooks diagnostics for
  subagentStart with the resolved child model. No global ~/.cursor/rules
  folder is assumed to load.
- OpenCode: live plugin entry and rendered instruction registered in the
  active JSON/JSONC config; installed native agents and copied skill paths.
- Hermes: registered skills and frozen policy section; observe global
  delegation-model limitations and API-model diagnostics.
- Pi: loaded extension, one skills discovery source, and cached policy body;
  subagent model control depends on the installed subagent extension.

Explicit-only skills may be available through slash invocation without appearing
in an implicit skill listing. Absence from that listing alone is not a failure.
Likewise, no guard records can mean no relevant calls, log rotation, or another
data directory. Do not assert a broken hook from silence alone.

For overhead, run:

```
python3 "<plugin-root>/scripts/measure_context.py" --json
```

This measures repository-controlled components, not all harness wrappers or
actual billed input. Inspect nearby user instructions for duplicates or
contradictions only when relevant; do not dump all memory into this task.
On a development checkout, `scripts/check.py` validates repository invariants.

Report concrete issues, evidence, and the smallest useful fix. Disclose what
could not be verified. Change nothing and run no paid smoke test unless the
user authorized it. Installation conflicts and malformed files need resolution,
not a blind `--force` retry. Never approve native hook trust on the user's behalf.
