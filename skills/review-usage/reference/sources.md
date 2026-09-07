# What each harness records, and what the scan corrects for

Loaded only by a run that is actually interpreting numbers. `usage_scan.py`
already applies every correction below; this file exists so a surprising figure
can be checked against the schema before it is reported as a finding.

## The three traps

Each one silently inflates a naive count, and each is corrected in the scan.

**Claude Code repeats usage per content block.** One API response is written as
several records — one per content block — each carrying an *identical* copy of
`message.usage` and sharing a `requestId`. Summing records double-counts a
thinking-plus-tool-use response twofold or more. The scan dedupes on
`requestId`, falling back to `message.id`.

Dispatches are the exception: a `tool_use` block lives in its own record, which
shares the `requestId` of the record carrying usage. So dispatches are collected
*before* the dedupe. If dispatch counts ever read zero while subagents exist,
that ordering has been broken.

**Codex's totals are cumulative.** `event_msg` / `token_count` carries both
`total_token_usage` (running, for the whole session) and `last_token_usage` (the
delta for that request). Summing totals is quadratic nonsense. The scan sums
deltas. Codex also reports `cache_write_input_tokens` as 0 in practice, so its
cache ratio is suppressed rather than printed as a huge meaningless number.

**OpenCode is multi-provider and stores milliseconds.** Times are epoch ms, not
ISO strings. Its `session` table is pre-aggregated per session and is the only
place in this whole file where a `cost` column is real money — and it may be
priced by a non-Anthropic provider, so it is never mixed into effective-token
comparisons. Subsessions are rows in the same table joined by `parent_id`, not
separate files.

## Where the data lives

| Harness | Source | Subagent linkage |
|---|---|---|
| claude | `~/.claude/projects/<slug>/*.jsonl`, plus `<session>/subagents/agent-*.jsonl` | sidecar `agent-*.meta.json` gives `agentType` and a `toolUseId` joining back to the parent's `tool_use.id` |
| codex | `~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl` | `sub_agent_activity` events carry an `agent_thread_id` |
| opencode | `~/.local/share/opencode/opencode.db` (SQLite, opened read-only) | `session.parent_id` |
| cursor, hermes, pi | nothing on disk in the usual locations | — |

Some sidecars are missing (roughly 2% on a long history), so an unknown
`agentType` is normal and never an error. Claude Code subagent transcripts carry
the *parent's* `sessionId`, so parent and child are separated by file path, not
by session id.

Windowing is by record timestamp, not file mtime — a long session spans days.
File mtime is used only as a cheap skip before opening a file.

## What is and is not comparable

- **Effective tokens** (`input×1 + cache_read×0.1 + cache_write×2 + output×5`)
  compare *groups within* this report. They are not dollars and not a bill.
- **Session counts** mean different things: Claude Code counts distinct
  `sessionId`s seen, Codex counts rollout files touched, OpenCode counts rows.
  Do not compare them across harnesses.
- **Cache ratios** are only meaningful where the harness reports cache writes.
- **`no data`** means the directory is absent — the harness is not installed
  here. It is not a failure and does not belong in a report as one.

## The guard log

`~/.leos-agent-local/dispatch.jsonl`, one JSON object per line, written by
`dispatch_guard.py` and summarised by `dispatch_log.py report`.

It stores **no prompt text and no paths** — prompts, sessions, and working
directories are truncated SHA-256. The prompt hash is what makes conversion
measurable: a blocked brief whose hash reappears with a tier named is a block
that worked. A blocked hash that never returns is abandoned work, and should be
reported as such rather than counted as a saving.

`decision: "error"` means the guard crashed and failed open — kept deliberately
distinct from a decision to allow, because conflating them is how a dead guard
goes unnoticed. A nonzero count is a bug, never a saving.

Zero rows from a harness that clearly ran sessions means the guard is installed
but not enforcing. On Codex that is the expected symptom of hash-pinned hooks
awaiting `/hooks` re-approval after an upgrade.
