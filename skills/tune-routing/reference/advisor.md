# Claude Code advisor — leos-agent

The advisor is Claude Code's server-side advisor tool. The main model decides
when to consult a second model, typically before committing to an approach,
on a recurring failure, or before declaring done. The advisor reads the whole
conversation and returns guidance; it edits nothing and runs no tools. That
makes it a consultant to the orchestrator, not a worker, so it does not
conflict with keeping the orchestrator at least as capable as its workers.

It is separate from tier routing. Tiers pick who does delegated work; the
advisor raises the quality of the main thread's decisions. The preference
lives only in Claude's own `advisorModel` setting, never in routing.json.

## Facts to check before recommending it

- Experimental, Anthropic API only: not on Amazon Bedrock, Claude Platform
  on AWS, Google Cloud's Agent Platform, or Microsoft Foundry. Through `ANTHROPIC_BASE_URL` it works only if the
  gateway forwards the tool intact. It also needs feature-flag fetching, so
  `DISABLE_TELEMETRY`, `DO_NOT_TRACK`, `DISABLE_GROWTHBOOK` or
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` keep it off, and
  `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` disables it outright. `doctor.py`
  reports all of these as the advisor's status.
- The advisor must rank at or above the session's main model; Claude Code
  skips one that ranks below, and subagents apply the same check against their
  own model. Accepted values are `fable`, `opus`, `sonnet`, or a full model
  ID. An organization `availableModels` list can exclude it.
- Cost: each consultation reads the full transcript at the advisor's rates,
  uncached, on top of the main model's usage. `usage_scan.py` counts it under
  the advisor's model. Claude Code's notice says it may use more tokens.
- Anthropic reports that a Sonnet main model with an Opus advisor scored 2.7
  points higher on SWE-bench Multilingual than Sonnet alone at 11.9% lower
  cost per task (claude.com/blog/the-advisor-strategy). That is Anthropic's
  claim, not a measurement by this project; review-usage cannot show savings
  without a comparable baseline.
- It fits long multi-step work where most turns are routine. On short tasks,
  or where every turn needs the stronger model, switch the main model instead.

## Guard and hooks

The advisor is a server tool with no name a hook matcher can reference, so
PreToolUse never fires for it: the dispatch guard neither sees nor routes it,
and the dispatch log records nothing for it. Its calls appear in transcripts
as `server_tool_use` blocks, which the usage scan does not count as dispatches.

## Changing it

`/advisor <model>` in a session writes the same key natively; `/advisor off`
removes it. The helper exists to show the exact change first and to write it
under the installer's safety rules:

```
python3 "<plugin-root>/scripts/claude_advisor.py" show
python3 "<plugin-root>/scripts/claude_advisor.py" set opus            # preview only
python3 "<plugin-root>/scripts/claude_advisor.py" set opus --apply    # on explicit request
python3 "<plugin-root>/scripts/claude_advisor.py" off --apply
python3 "<plugin-root>/scripts/claude_advisor.py" restore <backup>
```

`--scope local` targets this project's `.claude/settings.local.json` instead
of the user settings file. The helper edits only `advisorModel`, keeps every
other byte, follows a symlinked file, refuses malformed JSON, never creates a
settings directory, and writes a backup under the local data root first. A
change applies to new sessions, or after `/clear` or `/compact`.
