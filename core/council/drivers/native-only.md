# Driver: native-only (no external seats)

When `local/seats.<host>.json` has an empty `seats: []` (or is missing), the council runs in
**native-only mode**: every tier is served by independent read-only passes of the host's own model.

This is the zero-dependency baseline — no external CLI, no OpenRouter key. It **loses lineage
diversity** (the whole point of the council), so it is a fallback, not the target. When a review
runs native-only, the orchestrator MUST state the reduced-diversity caveat in its report.

Pass counts by tier: low = 1, elevated = 2, high = 3, critical = 3 (independent native passes).

To move off native-only, add external seats via the per-provider drivers (`claude-cli.md`,
`codex-cli.md`, `opencode.md`, `cursor-cli.md`) once each one's smoke test passes, and re-run setup
so it writes them (roster minus this host's own provider) into `local/seats.<host>.json`.
