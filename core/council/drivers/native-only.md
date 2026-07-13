# Driver: reduced-diversity fallback (no qualifying seat)

When no seat's `minTier` qualifies at the council tier — or `local/seats.<host>.json` has an empty
`seats: []` (or is missing) so zero seats are configured — the council runs in **reduced-diversity
fallback**: a single lowest-`minTier` configured seat runs once, the runner emits a `fallback-fired`
event, and the orchestrator MUST state in its report that diversity was reduced. If zero seats are
configured, the council is skipped (`skip`) instead.

This is the zero-dependency baseline — no qualifying external CLI, no OpenRouter key. It **loses
lineage diversity** (the whole point of the council): diversity = the count of distinct `provider`
values among the selected seats, and fewer than 2 distinct providers is reduced diversity. So this
fallback is a caveat, not a target. On a host with no seats file there is no setup-resolved
`{MODEL}`, so even a Codex host's fallback pass runs without `-m` — the OpenAI flavor pin applies
only to installed seat configs.

There is no longer a fixed 1/2/3/3 native-pass ladder; fallback runs **one** seat once regardless of
tier (the tier ladder's per-seat `minTier` selection is what governs pass counts in the normal
path — see DESIGN.md §4).

To move off reduced-diversity fallback, add seats via the per-provider drivers (`claude-cli.md`,
`codex-cli.md`, `opencode.md`, `cursor-cli.md`, `mimo.md`, `deepseek.md`) once each one's smoke test
passes, and re-run setup so it writes them into `local/seats.<host>.json` with per-seat `minTier`
values that qualify at the tiers you want covered.
