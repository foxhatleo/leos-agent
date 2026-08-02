# MCP executable pins

Leo setup executes only the exact core package versions declared together in
`models.json`'s `command` and `exactVersion` fields:

- `@upstash/context7-mcp@3.2.5`
- `@playwright/mcp@0.0.78`
- `chrome-devtools-mcp@1.6.0`
- `duckduckgo-mcp-server==0.5.0`

To update a pin, review the upstream release and its vendor-owned package
record, confirm that its command-line and MCP transport contract still match
setup's rendered shape, then change the command argument and `exactVersion`
in the same commit. Never use an unqualified package name, range, tag, or
`@latest`. Run both Python 3.9 and 3.14 setup suites, render adapters with
`--check`, and inspect a dry-run for every affected harness. The final diff
must receive the normal Opus-tier review before release.
