# Contributing

Thank you for improving Leo's Agent. Keep changes portable across its supported
harnesses — Claude Code, Codex, Cursor, and OpenCode — and keep generated output
derived from its source.

## Before opening a pull request

Use Python 3.9+ on macOS, Linux, or WSL, then run the canonical local checks:

```sh
python3 plugins/leo/scripts/render_adapters.py --check
python3 -m unittest discover -s tests -v
python3 tools/vendor/codex/validate_plugin.py plugins/leo
node tools/vendor/cursor/validate-template.mjs
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

Then re-run `--check`; generated drift is a failing change. Running the renderer
without `--check` also sweeps stale generated files, so removing a role or
declaring a native substitution for one deletes its adapters for you.

## Authoring a skill

A skill is two artifacts sharing a file. The frontmatter is a routing decision,
read constantly by a model deciding whether to open the body at all. The body is
a procedure, read rarely, and only once routing already succeeded. Most weak
skills are weak at the first job, and no amount of body quality compensates.

Since 8.0 this matters more, not less: nothing injects Leo's policy into a
session any more, so a description that fails to trigger is a skill that does
not exist.

### Frontmatter

| Key | Required | Notes |
|---|---|---|
| `name` | yes | must equal the containing directory name, exactly |
| `description` | yes | what it is and what it produces |
| `when_to_use` | for process skills | triggers *and* exclusions |
| `model`, `effort` | no | portable skills omit both |
| `disable-model-invocation` | no | blocks automatic triggering |
| `allowed-tools`, `argument-hint` | user-invoked only | command-shaped skills |

Anything outside that set fails the build. A portable skill should carry exactly
`name`, `description`, and `when_to_use`.

`description` and `when_to_use` together are the skill's **listing text** — the
only part of it loaded into every session whether the skill fires or not. Keep
the pair under roughly 450 bytes for a process skill;
`tests/test_token_budget.py` fails the build once the total passes the committed
ceiling. The two core policy skills are allowed more, because their listing text
carries the whole routing burden now that nothing is injected.

### Description and triggers

This is the highest-leverage part of the file.

The `description` says what the skill *is* and what it *produces*, in the third
person. It gets read out of context, sitting in a list beside dozens of others.

The `when_to_use` is a matched pair: the positive triggers, then the negative
ones, each pointing at where that case actually belongs. The negative half does
more work than the positive half — a skill with only triggers fires on
everything adjacent to them. Name the sibling skill in each exclusion, so the
reader is routed rather than merely turned away.

### The house shape

The existing skills share a spine, in this order:

1. `# <name>` and a core rule in the opening two or three sentences. Someone who
   reads only that paragraph should still be able to comply.
2. When it fires — and, just as explicitly, when it does not.
3. The mechanics: a phase table with exit criteria, a numbered discipline, or a
   procedure. Pick one; stacking all three makes none of them load-bearing.
4. Exemptions, where the skill warrants them.
5. Self-talk to catch — the rationalizations that come immediately before the
   violation, each answered in the same bullet. Write the sentence a reader will
   genuinely think, not a strawman.
6. Reviewable finding, where a reviewer should enforce it.
7. Works with — the neighbours, and what each of them owns, so the reader
   learns the boundary instead of the overlap.

### Exemption lists are closed

The signature of this set, and `leo:test-first` is the reference implementation.

- Numbered and bold-named, so a skip can cite one by name.
- Introduced as closed, with the no-analogy line. The failure being prevented is
  not skipping the rule outright; it is reasoning by resemblance into a skip.
- Each entry says why the underlying risk is absent, not merely that it is
  permitted.
- Closes with the reporting requirement: an unnamed skip is not a skip.

A skill may also deliberately have no exemptions — leo:visual-verification is
one. When so, say it plainly, because a reader arriving from a skill that has
them will otherwise read the absence as an oversight.

### Harness-specific prose belongs in the reference

Skill bodies must not branch per harness. Everything that differs between
harnesses — tier models, effort support, which tool does what, which Leo
component the harness already provides natively — is answered once in
`config/models.json` and rendered into
`plugins/leo/skills/routing/references/harnesses.md`. A skill points at the
relevant row there; it does not restate it.

This is what keeps a Claude session from paying for Codex's carve-outs, which
was 39% of the old injected policy.

### Where a personal skill goes

| Harness | Location |
|---|---|
| Claude Code | `~/.claude/skills/<name>/SKILL.md` |
| Codex | `~/.codex/skills/<name>/SKILL.md` |
| OpenCode | add the containing directory to the skills paths in `opencode.json` |
| Cursor | not confirmed here — check the harness's own documentation |

Plugin skills are namespaced `leo:<name>`; a personal skill is invoked bare, so
its name is free to collide conceptually without colliding literally. One row of
that table is unverified, which is why the advice is to confirm the load path
rather than trusting a path that may not exist — the same discipline
leo:freshness applies to a third-party API, turned on the harness.

Leo's own skills live in a plugin cache that every update overwrites. Never edit
one in place to customize it; write a personal skill instead.

### Registering a skill that ships with the plugin

Four places, all enforced, and a miss fails the build with a message that does
not obviously point at the omission:

1. The `SKILL.md` itself, with `name` matching its directory.
2. A row in leo:routing's skill index — keep it short.
3. At least one `leo:<name>` reference from some file other than its own body.
4. The roster constants in the test suite, plus the right root: portable skills
   in `skills/`, Claude-only skills in `skills-claude/`, and harness metadata
   under `agents/openai.yaml` only when that skill needs Codex invocation
   policy. Keep the `leo:` namespace in portable prose; OpenCode's generated
   copy uses `leo-<name>` because it has no namespace.

Write example tokens as `leo:<name>` with the angle brackets. A literal
placeholder like a made-up skill name is scanned as a real reference and fails
the build when it resolves to nothing.

A skill cannot be excluded from one harness on a whim. Codex's validator reads a
single `skills` directory and Cursor's requires one directory, so only Claude's
array-valued manifest and OpenCode's own tree walk can leave a skill out. The
renderer refuses a `drop` verdict anywhere else; use `prefer` there, which ships
the skill and points the session at the native instead.

## Layout and releases

`plugins/leo/` is the self-contained plugin payload. `plugins/leo/workflows/`
contains reusable workflow features; it is not GitHub Actions configuration.
Repository automation is in `.github/workflows/`, local release mechanics are
in `tools/release.py`, and pinned third-party validators live in `tools/vendor/`.

Release from an intentional `vX.Y.Z` tag. The release workflow validates,
builds the archive, stages the npm package, publishes npm when needed, and
creates or updates the GitHub Release. Do not maintain a hand-copied release
version in documentation; manifests are checked by `tools/release.py`.

Leo registers no MCP servers as of 8.0 and holds no credentials. Add servers
through each harness's own configuration.

For 8.0 upgrades: the memory store, `leo:setup`, and `leo:doctor` are gone, and
so is the injected policy block. Start with `leo:routing`, which is an ordinary
skill. Durable facts you kept in the memory store should move into the harness's
own memory surface — `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, a Cursor rules
file — which is where they were being copied to anyway. Machine-local ledgers in
`${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}` are untouched.

There is no project `AGENTS.md`: the portable policy lives in
`plugins/leo/skills/routing/SKILL.md`, `plugins/leo/skills/review-gate/SKILL.md`,
and the generated harness reference beside them.
