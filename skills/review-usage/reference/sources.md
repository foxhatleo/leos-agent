# Usage sources — leos-agent

The scanner reads local records as data. It never executes or prints prompt
text. A report describes retained observations, not complete provider billing.

| Harness | Source and normalization |
|---|---|
| Claude | Projects JSONL under CLAUDE_CONFIG_DIR or ~/.claude; main and subagent paths. Request/message IDs deduplicate repeated content blocks. Progressive usage snapshots keep maximum counters per response. Cache categories are already separate from ordinary input. |
| Codex | Rollout JSONL under CODEX_HOME or ~/.codex. Cumulative usage changes deduplicate repeated token events; pre-window events establish the baseline. Cached input is subtracted from total input. Output already includes reasoning. turn_context supplies model; session_meta subagent source attributes children. Missing cumulative data remains a gap. |
| OpenCode | Read-only SQLite under XDG_DATA_HOME or ~/.local/share. Per-message created time defines the window, not a session's last update. New session_message rows take precedence over mirrored message IDs. Cache categories are separate; stored reasoning is added to output once. Session parent_id attributes children. |
| Cursor, Hermes, Pi | Usage scanning is not implemented for their local schemas. Report unsupported; do not infer whether installed or used. |

Malformed rows and unknown timestamps are counted as gaps. Unsupported database
schemas report an error rather than successful zero usage. Interrupted requests
and provider activity absent from local logs cannot be recovered. Files deleted
or moved outside these locations are not included. Native forks that copy old
history without stable shared request IDs can limit attribution; the report
cannot independently establish provider charges for that copied history.

`model_usage` holds disjoint token categories per observed model and role.
`reference_cost` uses the bundled/cached OpenRouter catalog without a network
request. Exact, alias, estimated-neighbor, and unknown matches stay visible.
Conditional prices produce a range; missing cache rates leave unpriced tokens.
These are current reference text-token costs, excluding non-token fees,
negotiated discounts, historical price changes, subscription allocations, and
cache-lifetime differences the catalog does not encode. A zero priced subtotal
with unpriced tokens is not free usage. OpenCode's own cost field may itself be
computed from reference rates; it is not labeled an invoice.

The guard log is filtered by the same harness/window and rotates at a bounded
size. Requested/corrected/blocked decisions are not execution confirmation.
Only observed execution events count as confirmed. Missing rows may mean no
relevant dispatch, rotation, another data directory, or an unavailable hook.
Prompt hashes and burst timing are diagnostic hints, never causal savings.

Schema references:

- [OpenCode message tables](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/sql.ts)
- [OpenCode new message schema](https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/session-message.ts)
- [OpenCode token normalization](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/session.ts)
- [OpenRouter model catalog](https://openrouter.ai/docs/guides/overview/models)

Savings remain unmeasured until equivalent tasks are compared with adequate
quality checks. Reference prices and routing compliance alone are insufficient.
