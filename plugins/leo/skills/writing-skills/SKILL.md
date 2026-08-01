---
name: writing-skills
description: >
  How to author a skill in Leo's shape — the frontmatter keys and what the
  build enforces about them, a description and trigger pair that routes
  correctly, the closed-exemption-list structure the existing skills share,
  and where a personal skill file goes on each harness so it loads beside
  the plugin's own. Covers both skills that ship with the plugin and
  personal ones kept outside it.
when_to_use: >
  Writing a new skill, revising an existing one's frontmatter, or deciding
  where to put a personal skill so a harness picks it up. NOT for deciding
  whether a piece of process deserves to be a skill at all (that is
  leo:brainstorming), and NOT for plugin packaging or loader changes — this
  covers authoring one file and placing it.
---

# writing-skills

A skill is two artifacts sharing a file. The frontmatter is a routing decision,
read constantly by a model deciding whether to open the body at all. The body is
a procedure, read rarely, only once routing already succeeded. Most weak skills
are weak at the first job, and no amount of body quality compensates for it.

## Frontmatter

| Key | Required | Notes |
|---|---|---|
| `name` | yes | must equal the containing directory name, exactly |
| `description` | yes | what it is and what it produces |
| `when_to_use` | for process skills | triggers *and* exclusions |
| `model`, `effort` | no | portable skills omit both |
| `disable-model-invocation` | no | blocks automatic triggering |
| `allowed-tools`, `argument-hint` | user-invoked only | command-shaped skills |

Anything outside that set fails the build. A portable skill should carry exactly
`name`, `description`, and `when_to_use` — every process skill in this plugin
does.

## Description and triggers

This is the highest-leverage part of the file.

The `description` says what the skill *is* and what it *produces*, in the third
person. It gets read out of context, sitting in a list beside dozens of others.

The `when_to_use` is a matched pair: the positive triggers, then the negative
ones, each pointing at where that case actually belongs. The negative half does
more work than the positive half — a skill with only triggers fires on
everything adjacent to them. Name the sibling skill in each exclusion, so the
reader is routed rather than merely turned away.

## The house shape

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

## Exemption lists are closed

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

## Where a personal skill goes

| Harness | Location |
|---|---|
| Claude Code | `~/.claude/skills/<name>/SKILL.md` |
| Codex | `~/.codex/skills/<name>/SKILL.md` |
| OpenCode | add the containing directory to the skills paths in `opencode.json` |
| Cursor | not confirmed here — check the harness's own documentation |
| Hermes | no personal-skill directory; a skill here means a small local plugin |

Plugin skills are namespaced `leo:<name>`; a personal skill is invoked bare, so
its name is free to collide conceptually without colliding literally. Two rows
of that table are unverified, which is why the advice is to run leo:doctor and
confirm the load path rather than trusting a path that may not exist — the same
discipline leo:freshness applies to a third-party API, turned on the harness.

Leo's own skills live in a plugin cache that every update overwrites. Never edit
one in place to customize it; write a personal skill instead.

## Registering a skill that ships with the plugin

Four places, all enforced, and a miss fails the build with a message that does
not obviously point at the omission:

1. The `SKILL.md` itself, with `name` matching its directory.
2. A row in the policy's skill index — keep it short, since that table is
   injected into every session on every harness and the smallest budget wins.
3. At least one `leo:<name>` reference from some file other than its own body.
4. The roster constants in the test suite, and the skill list in the README.

Write example tokens as `leo:<name>` with the angle brackets. A literal
placeholder like a made-up skill name is scanned as a real reference and fails
the build when it resolves to nothing.

## Self-talk to catch

- "The description covers it, triggers are redundant" — the description sells,
  the triggers refuse. Without the refusal it fires on its neighbours.
- "I'll add an exemption for cases like this one" — "cases like this" is exactly
  the analogy a closed list exists to block. Name the case or do not exempt it.
- "This is a rule, not a skill" — if it has no procedure and no exemptions, it is
  a line in the policy, and the policy has a budget.
- "I'll copy the shape from another skill" — copy the structure, write the
  sentences fresh.

## Works with

- leo:doctor — confirm where this harness looks, and that it registered.
- leo:brainstorming — whether this should be a skill at all.
- leo:using-leo — where the index row goes, and what it costs.
