---
name: handoff
disable-model-invocation: true
description: Write the current session's context to a leos-agent handoff document so a later session — in this harness or another — can pick the work up with /handon.
argument-hint: "[what to emphasise]"
---

# /handoff — write a handoff document

A handoff is what survives when this session does not. It is read cold, by a
model with no memory of anything that happened here, possibly in a different
harness on a different day. Write for that reader.

`$ARGUMENTS`, when present, says what to **emphasise while writing** — "focus on
the caching work", "the installer is a dead end, say why". It steers this
document and is not stored; the handoff must stand alone without it.

Handoffs live at `${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/handoffs/<name>.md`.
That path is fixed and needs no plugin root.

`<plugin-root>`, where a step below uses it, is the directory holding
`rules/preferences.md`, from `$LEOS_AGENT_ROOT`, `$CLAUDE_PLUGIN_ROOT`,
`$PLUGIN_ROOT`, or the nearest ancestor of this file that contains it.

## Steps

1. **Pick a slug** — 2–4 words, kebab-case, naming the *work* and not the act of
   handing it off: `cache-aware-preferences`, `flaky-auth-retry`, not
   `session-handoff-2`. Then claim it:

   ```bash
   python3 "<plugin-root>/scripts/handoff.py" new <slug>
   ```

   It prints the de-collided name on the first line and the path to write on the
   second. Use the name it printed, not the slug you asked for — it may have
   appended a suffix.

   No plugin root to run it from? Do the same by hand rather than searching for
   the script: list `${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/handoffs/`
   and, if `<slug>.md` is taken, append `-2`, `-3` until it is not.

2. **Gather the frontmatter facts** in one batch:

   ```bash
   git rev-parse --abbrev-ref HEAD; git rev-parse --short HEAD; pwd
   gh repo view --json nameWithOwner -q .nameWithOwner
   ```

   `repo` may be absent (not a GitHub repo) — omit the key rather than guessing.
   `harness` is the one you are running in: `claude`, `codex`, `cursor`,
   `hermes`, `pi`, or `opencode`.

3. **Write the file** at the path from step 1:

   ```
   ---
   name: <the name step 1 printed>
   created: <ISO 8601 UTC>
   harness: claude
   repo: foxhatleo/leos-agent
   cwd: /Users/leoliang/workspace/leos-agent
   branch: main
   head: 16a724e
   ---
   # <one line: what this work is>

   ## Goal
   ## Done
   ## Next
   ## Key files
   ## Decisions
   ## Gotchas
   ```

   Sections, and what each is for:

   - **Goal** — what Leo is trying to achieve and why. Two or three sentences.
   - **Done** — what actually landed, with paths. Claims here need the same
     evidence a completion claim needs; "probably works" is a Gotcha, not a Done.
   - **Next** — the real next steps, in order, specific enough to start on.
   - **Key files** — path plus why it matters. Not a directory listing.
   - **Decisions** — settled calls and the reason, so the next session does not
     relitigate them.
   - **Gotchas** — traps, approaches already tried and rejected, and anything
     that only works in one harness.

4. **Report** the name and `/handon <name>`.

## Two rules that decide whether it is worth loading

**Pointers, not contents.** Name the file and say why it matters; the next
session reads it. A handoff that inlines code or a diff is stale the moment
anyone commits, and it costs a fresh context to load something it could have
read itself.

**Harness-portable.** The reader may be on Codex or OpenCode. Anything that only
works here — the `Monitor` tool, `attach-pr`, a Claude-only skill — gets said
out loud as harness-specific rather than assumed.

Keep the body under about 100 lines. A handoff longer than that is a session
transcript, and the next session will pay for it on every turn.

## Housekeeping

Nothing is pruned automatically. `handoff.py list [--all]` shows what exists and
`handoff.py rm <name>` deletes one.
