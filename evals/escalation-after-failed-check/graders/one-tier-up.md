---
type: tool_used
tool: Agent
input_match: '"model"\s*:\s*"(?:sonnet|claude-sonnet[^"]*)"|^(?!.*"model"\s*:)(?=.*"subagent_type"\s*:\s*"(?:leos-agent:)?leo-(?:standard|executor)")'
min: 1
arm: with-only
---
