@AGENTS.md

## Claude Code

Everything in `AGENTS.md` applies. Claude-specific notes for working IN this repo:

- The council's **native** seat on a Claude Code host is a read-only Agent subagent **pinned to
  `model: opus`** — the Opus line specifically, never Fable or Mythos, even if this session runs a
  different model. External seats are {GPT, GLM, Gemini, Grok}.
- Claude reads this `CLAUDE.md`; the canonical content is `AGENTS.md` (imported above). Keep new
  guidance in `AGENTS.md` so every host gets it — add here only genuinely Claude-only deltas.
