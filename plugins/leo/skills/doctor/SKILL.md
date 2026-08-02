---
name: doctor
description: >
  Self-check for Leo's own wiring. Reports which harness this is, what each
  tier name resolves to here, whether the bootstrap is installed, where
  machine-local state and the memory store live, and which skills shipped
  versus which this session can actually invoke. Disk facts come from a
  helper script; the context facts only the running session can answer, and
  a disagreement between the two columns is the diagnosis. Use when Leo asks
  about Leo's loading, routing, or skill wiring. Do not use for project health
  checks, project-code debugging, or unprompted inspection.
when_to_use: >
  Leo asks whether the policy loaded, why routing or a skill is misbehaving,
  or invokes doctor by name after installing, updating, or switching harness.
  Also the first move when a leo skill cannot be found. NOT a general
  environment or project health check, NOT for debugging the project's own
  code (that is leo:debugging), and never run unprompted — it reports on the
  agent, not on the work.
---

# doctor

Doctor answers two questions that look like one: what shipped to disk, and what
reached this session. A skill the harness never registered is indistinguishable
from a skill that does not exist, right up until the moment you invoke it.

## Run the script

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py" --harness <name>
```

Pass `--harness` with the harness you are on — the mapping appendix in your
context names it in its own heading (`# Hermes mapping` → `hermes`). Detection
without it relies on a plugin-root variable that Hermes and OpenCode do not
export, so on those two the script reports `unknown` rather than guessing.
`unknown` on a harness whose mapping you can plainly read is a missing
argument, not a fault.

`${CLAUDE_PLUGIN_ROOT}` is the Claude Code spelling. Codex exports
`$PLUGIN_ROOT` and Cursor `$CURSOR_PLUGIN_ROOT`. On Hermes and OpenCode no
plugin-root variable exists at all — the injected policy instead substitutes an
absolute payload path into its `state.py` and `memory.py` commands. Read that
command path from the policy as the discoverable source. Being unable to locate
the payload at all is itself the first finding: the harness is not looking where
the plugin was installed.

Add `--json` when you want the same facts as data.

Doctor validates the bootstrap that actually belongs to the named harness:
the session hook and manifest for Claude, Codex, and Cursor;
`config.instructions` in OpenCode's plugin; and Hermes registration plus its
first-tool-result fallback. It also reports the running Python version against
the supported 3.9+ floor. Codex hook trust is not provable from disk: review
the plugin in `/hooks` and confirm it is trusted before treating the on-disk
hook as active.

## Then answer the three it cannot

A script can prove the hook is installed and that the policy renders. It cannot
prove the policy arrived. Only you can see your own context.

1. **Did the policy load?** Look for the policy wrapper in your context, and
   check that the mapping following it names *this* harness. A policy present
   but carrying another harness's mapping is worse than none, because routing
   then points at models that do not exist here.
2. **Which skills are actually invocable?** Compare your own skill list against
   the script's shipped roster. Mind the naming rule: most harnesses namespace
   them as `leo:<name>`, while OpenCode has no namespace and requires a
   skill's frontmatter name to match its directory, so the plugin registers a
   generated shadow copy with every skill renamed `leo-<name>`. A skill that
   looks missing on OpenCode may simply be listed as `leo-<name>` rather than
   `leo:<name>`.
3. **Is memory present and delivered?** The script reports whether the store
   exists and whether each native surface received its generated copy. Whether
   those facts are in front of you right now is something only you can confirm.
   Report the two separately; they disagree more often than expected. Hermes's
   projection is opt-in, so doctor reports it explicitly as disabled rather
   than silently omitting it.

## Reading the report

Every row carries its source — `env`, `disk`, `config`, or `context` — so a
reader can tell a fact from an inference. Close with one verdict from exactly
three: **healthy**, **degraded**, or **not loaded**. Never free prose. `not
loaded` outranks everything else: if the policy did not arrive, nothing else in
the report describes how this session will actually behave.

**Most breadcrumb logs are history, not a verdict.** Some older logs carry no
timestamps, and the test suite drives failure paths deliberately, so entries
can accumulate on a development machine. Quote the newest line if useful, but
never conclude "the hook failed this session" from history alone. The one
capability exception is `opencode-skills.log`: its presence means namespaced
OpenCode skill registration degraded and doctor reports that state until the
breadcrumb is cleared after the underlying problem is understood.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Policy absent, bootstrap installed | the hook fired and failed open | read the newest breadcrumb, then confirm it describes this session before believing it |
| Policy present, mapping names another harness | detection resolved wrong, usually a stray plugin-root variable exported in an unrelated shell | unset it, restart the session |
| Harness reported as `unknown` | no `--harness`, and this harness exports no plugin-root variable | degraded until re-run with `--harness <name>` read off your mapping heading; a still-unknown explicit run is invalid wiring |
| Codex hook is on disk but policy is absent | the new or changed hook may not be trusted | open `/hooks`, review the hook, and explicitly trust it |
| OpenCode reports `opencode-skills.log` | the namespaced shadow tree failed and no bare-name fallback was registered | inspect the newest breadcrumb, fix the path/permission failure, and restart OpenCode |
| Shipped roster exceeds what you can invoke | the harness cached an older payload, or the skills directory is not registered | update the plugin; on OpenCode check `opencode debug skill` for each skill's `location` |
| Skills listed as `leo-<name>` instead of `leo:<name>` | OpenCode, working as designed | invoke them as `leo-<name>`; not a fault |
| Tier names resolve to models this harness cannot run | mapping and harness disagree | same as row 2 |
| Machine-local state not writable | the path override points somewhere unwritable | fix or unset it |
| A skill is genuinely absent from disk | it was never added | see leo:writing-skills |

## Doctor never repairs

It reports, and it names the fix. It does not reinstall, rewrite configuration,
or delete state — which is what keeps it safe to run at any tier and at any
moment.

## Works with

- leo:writing-skills — for a skill that turned out to be missing because nobody
  wrote it yet.
- leo:memory — doctor reports whether the store exists and reached each surface.
- leo:verification — this report is a claim like any other: the script ran this
  turn and its output was read.
