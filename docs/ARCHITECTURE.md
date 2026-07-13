# Architecture & rationale

Why leos-agent is shaped the way it is. (Committed on purpose — the design-of-record must not live
only in a gitignored scratch dir.)

## Why one repo

Leo previously ran two near-identical config repos (`leos-claude`, `leos-codex`) with no shared
source of truth. A hardening round reached one and not the other at the same version — a silent
security regression (the weaker guard was the one installed). A single `core/` makes that class of
drift **physically impossible**: there is one `bash-guard.py`, one council engine, one skill, one
policy dataset. Four harnesses now converge on shared standards (`AGENTS.md` instructions, the
`SKILL.md` Agent-Skills format, PreToolUse-style command hooks), so the per-host surface is thin
adapters, not forks.

## Maximum sharing

- **Instructions:** one canonical `global/AGENTS.md`, delivered to each host by an **additive
  reference** — never a whole-file symlink, which would clobber a user's own global instructions:
  - **Claude** — a managed `@import` block in `~/.claude/CLAUDE.md` (Claude resolves `@import`
    natively; coexists with the user's content; live on pull). Written by `bin/leos-block.py`.
  - **Codex** — a `SessionStart` hook that injects `global/AGENTS.md` as `additionalContext`.
    (Empirically verified: Codex `@import`/`@./path` is inert, and `model_instructions_file`
    *replaces* the base/system prompt — both unusable; the hook is additive and base-safe.)
  - **OpenCode** — an `instructions[]` entry in the merged `opencode.json` (absolute clone path via
    the `{{CLONE_ROOT}}` merge token; additive with the user's `AGENTS.md`).
  - **Cursor** — no global file; per-project `AGENTS.md` only.
- **Council:** one `SKILL.md` in `~/.agents/skills` (read natively by Codex, OpenCode, Cursor) +
  `~/.claude/skills/council` for Claude. One engine, data-driven from `local/seats.<host>.json`.
- **Guard:** one `bash-guard.py` core + a thin per-host registration (Claude/Codex PreToolUse
  exit-2, Cursor `beforeShellExecution` shim, OpenCode plugin).
- **Policy:** one `policy-data.json`, rendered per host, with enforced-vs-advisory labels so the
  boundary is explicit and never silently crossed.

## Delivery: hybrid symlink

Symlinks retire the copy/ownership/migration machinery for everything a host does NOT rewrite. The
gains and the guardrails:

- **`git pull` updates linked payload immediately.** Stable installations should point links at a
  release worktree; a development-clone install explicitly accepts that checkout/rebase/pull swaps
  active hooks. Refresh the private runtime and reapply every merge drift reported by doctor.
- **Self-location.** Hooks/engine find `<clone>/local/` via `realpath(__file__)`, so one symlinked
  script serves any tool home.
- **Everything machine-local lives in the clone, gitignored** (`local/`) — including the council
  seats. This is Leo's explicit choice: config is asked at setup and saved in the clone (never in a
  tool home, never committed), which also sidesteps the "don't symlink a machine-local file into the
  repo" hazard.
- **Merge protects host-owned registries/config.** Claude settings, Codex config/hooks, OpenCode
  config, and Cursor config/hooks use ownership-aware merges. `leos-merge` preserves unrelated TOML
  comments/order, snapshots first, refuses conflicts, and records drift. `leos-uninstall` removes
  only values still matching that ownership snapshot; backups are recovery, never rollback state.
- **Fail-closed guard wrapper.** A dangling guard symlink exits 2 (blocks) rather than silently
  passing; the formatter/council hooks fail open (availability over safety, since they aren't
  tripwires).
- **Release worktree (recommended).** Create a separate worktree at a reviewed tag/commit, run the
  full batteries there, configure hosts from that path, and advance it only after validating the
  next release. Pointing at the development clone is explicit live-update mode.

  A concrete stable rollout is: `git worktree add --detach ~/.leos-agent-release <reviewed-sha>`,
  bootstrap that worktree's `local/.venv`, run every setup battery there, then run its link/merge
  commands. For an upgrade, create and validate a second release worktree first, repoint links with
  its `leos-link --force` only after approval, reapply merges, run doctor, and remove the old
  worktree only after live host checks pass. Never move a host-facing worktree while tests run.

## Council: unified `seats[]`, seven flagship roles

Seven flagship roles are fixed (opus, gpt, glm, gemini, grok, mimo, deepseek); the exact model
slug is resolved at setup and never committed (provider versions change). OpenAI resolves to the
most capable flavor of the newest GPT generation — GPT-5.6 ships Sol > Terre > Luna, so 5.6 → Sol,
superseded automatically by the next GPT generation (Leo's standing rule). The Anthropic seat is
always the **Opus line** — never Fable or Mythos.

The host's own-provider seat is **no longer first-class** (there is no top-level `native` object):
it is one element of the unified `seats[]` array in `local/seats.<host>.json`, with `mode` in
{`subagent`, `exec`}. On a Claude Code host the own-provider (Opus) seat is `mode: subagent` — an
in-process read-only Agent subagent pinned to `model: opus` (the only harness with a true subagent
primitive + `--safe-mode`); the runner reports it as `orchestrator-subagent-required` and the
orchestrator dispatches it and folds it back via `runner.py collect-subagent` (`collect-native` is
kept as a legacy alias). On every other host the own-provider seat is `mode: exec` — a runner
subprocess reusing the host's login (Codex-on-Codex, Cursor-on-Cursor, OpenCode-on-OpenCode);
doctor reports `kind: "exec"` (was `"external"`).

**Per-seat `minTier`** (integer 1..4, default 4) replaces the old positional native+external
ladder: a seat runs at council tier T iff `seat.minTier <= T`, for BOTH plan and impl checkpoints.
The engine reads `minTier` ONLY from the installed seats file — never assumes defaults. The
catalog's `presets.minTier` block (opus=1, gpt=2, grok=3, glm/gemini/mimo/deepseek=4) is a
setup-time default the installer stamps; a hand-edited seats file may override it. If no seat
qualifies at the tier, the runner runs the single lowest-`minTier` configured seat once, emits
`fallback-fired`, and the report states reduced diversity (distinct-provider count < 2); zero seats
configured → council skipped (`skip`).

**Per-seat env file** is the secret channel: `local/council/env/<seat>.env` (gitignored, mode 0600),
loaded by the runner at dispatch, contents never entering prompts/logs/`result.json`. The inline
`env` dict on a seat is non-secret only (secret-named keys TOKEN/SECRET/PASSWORD/API_KEY refused at
install); secret-named keys ARE allowed in the envFile. Enforcing hosts deny the LLM reading
`**/council/env/**` via policy.

### Anti-recursion (a seat never convenes its own council)

Deterministic first, tool-agnostic:

1. The explicit runner sets `LEOS_COUNCIL_SEAT=1` on every `mode: exec` seat launch (inherited by child hooks).
2. The Stop hook returns 0 immediately when the sentinel is set.
3. The skill, runner, and prompts refuse a nested Leo's Agents council. Seats may still use ordinary
   host subagents; this is a recursion boundary, not a ban on delegation.
4. One shared clone-local `STATE_ROOT` under `local/council/state` + an owned in-review marker
   cover env-stripping CLIs and cross-tool visibility without using `/tmp` or `~/.local/state`.
5. Per-seat controls: Claude uses `--safe-mode --no-session-persistence`; Codex uses `--ephemeral`
   and read-only sandbox while retaining normal `CODEX_HOME` authentication (**never** override
   `CODEX_HOME` on a codex seat — isolation is `--ephemeral` + scratch cwd + the sentinel); other
   transports use documented plan/read-only modes and disclose when session persistence cannot be
   disabled.

The runner creates its owned marker before dispatch. Its vendor-neutral detached
`start`/`status`/`stop` lifecycle survives host tool-call deadlines while retaining explicit
cancellation; synchronous `run` remains for compatibility. It records bounded stdout/stderr, exit
code, elapsed time, and a typed terminal state. A blank/invalid/nonzero/timed-out CLI call is never
treated as a successful review.

Only the `Stop` event is registered (never `SubagentStop`), so subagent seats are never
hook-nudged. Backstops retained: 2-nudge loop guard, read-only seats, per-seat timeouts, 2-pass cap.

## Honest boundaries (do not paper over)

- **Codex secret-reads are advisory**, not pattern-enforced (no declarative read-deny surface);
  coverage is hook/sandbox-mediated. The policy renderer labels this; a CI contamination check
  asserts Codex/Cursor config never contains a literal Claude permission string.
- **Cursor CLI headless hooks/skills are version-sensitive.** Static config covers the secret-read
  denies verified by setup; it is not advertised as a catastrophic-shell guard. Smoke-test
  `beforeShellExecution`; if it does not fire, report reduced shell protection.
- **OpenCode has no Stop-event hook**, so there is no automatic council nudge there — the council
  runs via the skill + the global `AGENTS.md` mandate.
- **Model slugs and Cursor's Grok slug** must be resolved at setup (`cursor-agent --list-models`);
  nothing here pins them.

## What deliberately does not exist

A `VERSION` file as a migration trigger, `MIGRATE.md`, `RECONCILE`-as-reinstall, ownership-sha
hashing, a copy-based installer, and per-tool prose forks. `CHANGELOG.md` is human history only.
The seats-schema redesign (unified `seats[]`, no top-level `native`, per-seat `minTier`) is a
breaking change detected by `leos-doctor.py` on old-shape `seats.<host>.json` (top-level `native`,
missing `mode`/`minTier`); doctor prints "regenerate via SETUP step 5 + `leos-seats.py write`" and
refuses. This is the documented precedent for a schema change — a doctor-detected regeneration
requirement, not a version-gated migration system. `setup --refresh` is a no-op for the redesign
(no new deps).
