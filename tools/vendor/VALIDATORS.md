# Vendored release validators

These files are checked into this repository so release validation does not
download executable code at release time.

## Codex plugin validator

- Source: https://github.com/openai/codex/blob/582569998181aad08a88bacc151a94b2048a5d1f/codex-rs/skills/src/assets/samples/plugin-creator/scripts/validate_plugin.py
- Commit: `582569998181aad08a88bacc151a94b2048a5d1f`
- Git blob SHA: `88fae0fd00998ea32fa2393869042f0231a2b43b`
- File SHA-256: `ebda00d55d7518b127f675f062fb5c6e7a1ffdc0a99df1a55ac594400d7d3228`
- License: Apache-2.0 (OpenAI Codex repository)

## Cursor plugin validator

- Source: https://github.com/cursor/plugin-template/blob/46216072ac5750f782f95bb325b4d12b7c3ae9c9/scripts/validate-template.mjs
- Commit: `46216072ac5750f782f95bb325b4d12b7c3ae9c9`
- Git blob SHA: `5310b9e8743213a7ac6c014d743bb03917dcf020`
- File SHA-256: `826f55f546ce59500a6e3d7d32a15d90f3373cecc3b41486e75ae28b60647a4a`
- License: MIT (Cursor plugin-template repository)

## Update procedure

Choose an explicit upstream commit, download the exact blob, verify both its
Git blob SHA and file SHA-256, replace the vendored file, and update every
provenance field above.
Run `python3 -m unittest tests.test_release -v` before relying on it in a
release. Do not replace these files with a mutable branch URL or a pipe to a
shell/interpreter.
