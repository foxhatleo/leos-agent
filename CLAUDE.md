# leos-agent

Read [AGENTS.md](AGENTS.md) first. It is the canonical guidance for working in
this repository: project intent, gates, context-budget rules, harness facts,
release flow, and the decisions already settled with Leo. Keep it and README.md
in agreement; do not duplicate its content here.

Claude Code specifics that AGENTS.md covers only briefly:

- This repo is also the plugin installed in your session. Editing files here
  does not change the loaded copy; the plugin cache serves the last installed
  version, and a same-version reinstall keeps serving it.
- `hooks/hooks.json` and `agents/` are auto-discovered. Never add them to
  `.claude-plugin/plugin.json`.
- Agent types from the installed plugin are namespaced, for example
  `leos-agent:leo-cheap`. Use those names when delegating from this repo.
