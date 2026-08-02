# Vendored release validators

These files are checked into this repository so release validation does not
download executable code at release time.

## Codex plugin validator

- Source: https://github.com/openai/codex/blob/582569998181aad08a88bacc151a94b2048a5d1f/codex-rs/skills/src/assets/samples/plugin-creator/scripts/validate_plugin.py
- Commit: `582569998181aad08a88bacc151a94b2048a5d1f`
- Git blob SHA: `88fae0fd00998ea32fa2393869042f0231a2b43b`
- File SHA-256: `ebda00d55d7518b127f675f062fb5c6e7a1ffdc0a99df1a55ac594400d7d3228`
- License: Apache-2.0 (OpenAI Codex repository), included verbatim at
  `tools/vendor/codex/LICENSE`
- License source: https://github.com/openai/codex/blob/582569998181aad08a88bacc151a94b2048a5d1f/LICENSE
- License Git blob SHA: `4606e72e042564097e8780d66c1d4dcb611869bd`
- License SHA-256: `d17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc`

## Cursor plugin validator

- The validator is independently authored for this repository; it is not a
  vendored Cursor source file.
- The upstream repository declares no license at Cursor `plugin-template`
  commit `46216072ac5750f782f95bb325b4d12b7c3ae9c9`. The previously copied
  blob was removed rather than relicensed.
- The replacement is governed by this project's MIT license.

## Update procedure

For a vendored validator, choose an explicit upstream commit, download the
exact blob, verify both its Git blob SHA and file SHA-256, replace the
vendored file, and update every provenance field above. Independently authored
validators must instead retain their author and project-license provenance.
Run `python3 -m unittest tests.test_release -v` before relying on it in a
release. Do not replace these files with a mutable branch URL or a pipe to a
shell/interpreter.
