# Contributing

Thank you for improving Leo's Agent. Keep changes portable across its supported
harnesses and keep generated output derived from its source.

## Before opening a pull request

Use Python 3.9+ on macOS, Linux, or WSL, then run the canonical local checks:

```sh
python3 plugins/leo/scripts/render_adapters.py --check
python3 -m unittest discover -s tests -v
python3 tools/vendor/codex/validate_plugin.py plugins/leo
node tools/vendor/cursor/validate-template.mjs
git ls-files --error-unmatch tests/test_setup.py
git diff --check
```

Run `claude plugin validate .` as well when Claude Code is installed. The
vendored validators are intentional: do not replace them with a mutable
download at validation time. Their pinned provenance and update procedure are
in [`tools/vendor/VALIDATORS.md`](tools/vendor/VALIDATORS.md).

Release maintainers additionally run the full suite on Python 3.9 and 3.14,
then exercise packaging without mutating the source tree:

```sh
python3.14 -m unittest discover -s tests -v
python3 tools/release.py --check-version vX.Y.Z
python3 tools/release.py --build /private/tmp/leo-release-check
python3 tools/release.py --stage-npm /private/tmp/leo-npm-check
npm pack /private/tmp/leo-npm-check --dry-run --json
```

Replace `vX.Y.Z` with the exact prospective release tag, which must match all
manifests. The `/private/tmp` paths are macOS examples; on Linux or WSL use
fresh directories beneath a secure temporary directory.

Edit canonical role prompts and `plugins/leo/config/models.json`, not rendered
adapters or `plugins/leo/README.md`. Re-render with:

```sh
python3 plugins/leo/scripts/render_adapters.py
```

Then re-run `--check`; generated drift is a failing change.

MCP executable versions are exact reviewed pins. Follow
[`plugins/leo/config/MCP_PINS.md`](plugins/leo/config/MCP_PINS.md) when updating
one; never replace a pin with `latest`, a range, or an unqualified package.

## Layout and releases

`plugins/leo/` is the self-contained plugin payload. `plugins/leo/workflows/`
contains reusable workflow features; it is not GitHub Actions configuration.
Repository automation is in `.github/workflows/`, local release mechanics are
in `tools/release.py`, and pinned third-party validators live in `tools/vendor/`.

Release from an intentional `vX.Y.Z` tag. The release workflow validates,
builds archives, stages the npm package, publishes npm when needed, and creates
or updates the GitHub Release. Do not maintain a hand-copied release version in
documentation; manifests are checked by `tools/release.py`.

For 7.0 upgrades, move any old `LEOS_AGENT_PATH` state from its nested
`local/` directory into `${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/`
and rename the environment variable. If Leo seems absent after an update, start
a new session and run `leo:doctor`; on Codex, also review `/hooks` trust.

There is no project `AGENTS.md`: the portable policy lives in
`plugins/leo/skills/using-leo/SKILL.md` and its generated harness mappings.
