# jsonc-parser provenance

OpenCode configuration edits use `jsonc-parser` 3.3.1's `modify` plus
`applyEdits` operations through `scripts/jsonc_bridge.cjs`. It is vendored
rather than loaded at setup time: setup must be deterministic, work offline,
and must not turn a user-approved config edit into an unreviewed mutable
install.

Reviewed registry artifact: [jsonc-parser 3.3.1](https://registry.npmjs.org/jsonc-parser/-/jsonc-parser-3.3.1.tgz), MIT.

- npm SHA-1: `f2a524b4f7fd11e3d791e559977ad60b98b798b4`
- npm integrity: `sha512-HUgH65KyejrUFPvHFPbqOY0rsFip3Bo5wb4ngvdi1EpCYWUQDC5V+Y7mZws+DLkr4M//zQJoanu1SP+87Dv1oQ==`
- fetched tarball SHA-256: `4a0315b8671e7463bae7af7c142cdf19e9aa7ba39eb36dc2df383b8648e3cbc9`

The vendored files are `LICENSE.md`, `package.json`, and the complete
dependency-free `lib/umd/` runtime used by the bridge.

Update procedure: download that exact registry tarball, verify its SHA-256
against the reviewed value above, replace only those files, then run both
`python3 -m unittest tests.test_setup` and `python3.14 -m unittest tests.test_setup`
plus `python3 -m unittest tests.test_release`. Review the new release and
record its exact registry URL, SHA-1, SHA-256, integrity, and version here.
Core MCP executable pins have their separate review procedure in
`config/MCP_PINS.md`. Preserve the add-only rule, comments, trailing
commas, indentation, symlinks, and file modes. Never replace a JSONC edit with
`JSON.stringify` or `json.dumps`.
