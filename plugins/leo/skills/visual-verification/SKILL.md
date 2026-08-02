---
name: visual-verification
description: >
  Render-evidence gate for changes a person can see. A UI-visible edit is not
  reported done until a render produced after the edit has been looked at.
  Detection walks a ranked ladder of whatever browser, preview, or simulator
  tooling this harness exposes; when nothing on the ladder answers, the change
  is reported with an explicit unverified warning instead of a completion
  claim. Use when a person can see the changed result. Do not use for
  non-rendered logic, an off feature flag, or as a replacement for tests.
when_to_use: >
  A change whose result someone would notice by looking — layout, styling,
  on-screen text, a new view or route, a chart, a generated image or rendered
  document. Fires just before the completion claim, beside leo:verification.
  NOT for logic with no rendered surface, NOT for a component behind a flag
  that is off, and NOT a replacement for tests — a render shows one state, a
  test covers the branch.
---

# visual-verification

A pixel claim needs a pixel. Reporting that a visible change works, having
never rendered it, is an assertion about something nobody looked at — and the
whole suite can be green while the element sits clipped, transparent, or
underneath its own container.

This is the complement of the test gate. leo:test-first exempts pure copy and
styling tweaks precisely because a test would say nothing useful about them;
this skill is what catches them instead. What one gate waves through, the other
holds.

## When it fires

Rendered layout or styling; on-screen text; a new or changed view, route, or
component; a chart, canvas, or generated image; a rendered document artifact;
a state someone reaches by clicking.

It does not fire for data-layer changes, logging, build configuration, or a
component behind a disabled flag.

## The detection ladder

Walk in order, stop at the first rung that answers. A rung that exists but
errors or returns nothing counts as absent for this purpose.

1. **A harness-native preview or browser pane** — starts or attaches to the
   project's own dev server and screenshots the running app. Highest fidelity:
   it renders the real build.
2. **A harness-native attached browser** — drives an already-running browser at
   a URL. Right when the app is deployed or served outside this session.
3. **A platform simulator** — for native UI no browser can show.
4. **A scriptable driver through the shell** — Playwright or Puppeteer, or an
   existing end-to-end test that captures a screenshot. Check the lockfile
   before concluding the project does not have one.
5. **A rendering assertion the project already owns** — a snapshot or visual
   regression suite. Weaker than a render you looked at, but it is evidence
   produced after the edit. Name which one you used.

**The ladder is about capability, not brand names.** A harness that renames its
browser tool still has rung 1. Where a harness defers part of its tool
inventory until it is searched, an empty tool list is not evidence of absence —
search first, then conclude. That distinction is the most likely way this gate
degrades to a warning when something was in fact available.

The mapping appended to the session policy names which rungs exist here.

## What counts as evidence

The render is produced **after** the edit, this turn, and is actually looked
at. Name what you checked in it — the element, where it sits, what state it is
in. A screenshot captured is not a screenshot read.

## When nothing answers

There is no exemption list here. A UI-visible change either carries a render or
carries this block. Emit it **instead of** the word done:

```
UNVERIFIED UI CHANGE — no render tool answered on this harness.

Changed:    <the visible change, one line>
Expected:   <what should look different, and where>
Probed:     <the rungs tried, by name, in order>
Verify by:  <the one concrete thing Leo can do — a URL, a command, a screen>
```

The completion line then reads "implemented, unverified" — never "done". A
`Probed:` line that names nothing means the ladder was skipped, not that it came
up empty. Never suppress the block because the change looks obviously correct
or the diff was one line of CSS.

## Self-talk to catch

- "It's one line of CSS" — one line of CSS is what collapses a flex container.
- "The component tests pass" — tests assert a tree; an element can be present
  and invisible.
- "There's no browser tool here" — did you probe, or read a tool list that
  hides half its inventory until asked?
- "I'll mention it wasn't verified in passing" — in passing is how it gets read
  as done. Use the block.
- "I rendered it earlier" — then you have a picture of the previous version.

## Reviewable finding

A UI-visible diff reported done with neither render evidence nor the warning
block is a blocking finding.

## Works with

- leo:verification — the same rule about evidence being fresh, applied to a
  render rather than an exit status. That gate owns the completion claim; this
  one owns what a visible claim needs behind it.
- leo:test-first — its copy-and-styling exemption is this skill's inbox. A
  snapshot test is rung 5 and satisfies both, once.
- reviewer — the warning block is an artifact to judge, not prose to skim past.
