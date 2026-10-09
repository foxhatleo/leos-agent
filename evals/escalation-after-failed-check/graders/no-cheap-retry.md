---
type: tool_used
tool: Agent
input_match: '"model"\s*:\s*"(?:haiku|claude-haiku[^"]*)"|^(?!.*"model"\s*:)(?=.*"subagent_type"\s*:\s*"(?:leos-agent:)?leo-(?:cheap|runner)")'
min: 0
max: 0
arm: with-only
---
