# Native model mapping — leos-agent

Use account-visible native model IDs. Read-only model lists/pickers are useful;
none proves that a future child request will succeed under organization rules.

| Harness | Selection and application |
|---|---|
| Claude | Agent accepts haiku, sonnet, opus, or fable aliases; configure these aliases, not full transcript model IDs. The guard corrects a missing/known-overpriced choice without granting tool permission. Forced settings and provider substitution need actual-model checks. |
| Codex | Native model picker and active config. Installer writes model-free user agent TOMLs. The guard requires the selected tier model and configured effort at spawn, with a parent-price ceiling. Other/customized profiles may override spawn settings. Native hook trust remains required. |
| Cursor | Account model picker; installer writes ~/.cursor/agents profiles. Resolved subagentStart model is checked before launch. Do not assume a Task model field or a loaded ~/.cursor/rules directory. |
| OpenCode | Native model list/provider config; installer writes native agents and one instruction entry. Task selects a subagent_type, not a model argument; the adapter queries actual registered agents before switching profiles. |
| Hermes | Active profile's delegation.model is global to child work. Separate per-task cheap/standard/premium selection is unavailable. The adapter observes parent models and checks known global child costs. |
| Pi | Native model listing plus the installed subagent extension's documented schema. Core extension hooks alone do not establish a per-spawn model field. Report unsupported routing honestly. |

Claude defaults: cheap Haiku, standard Sonnet, premium Opus. Codex defaults:
cheap GPT-5.6 Luna, standard GPT-5.6 Terra, premium GPT-5.6 Sol. Reasoning effort
is optional configuration; routing does not set a default effort. These are
capability defaults, not guaranteed price rankings. The current parent is
always the price ceiling where comparison is known, so Terra can be clamped to
Sol when Sol is the parent. Never raise a cheap parent's child deliberately.

Other harnesses require explicit available model mappings for native profiles.
Rerun the installer after changes, then start/reload the native harness.
Configured labels on an unsupported interface remain advisory; do not write a
fictional API field to make a test appear to pass.

Price lookup preserves requested IDs. Exact/alias/estimated/unknown status and
catalog age belong in diagnostics. Paid probes need explicit authorization and
a budget; fixture tests and registration checks do not call models.
