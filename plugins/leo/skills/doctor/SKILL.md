---
name: doctor
description: >
  Self-check for Leo's own wiring. Reports which harness this is, what each
  tier name resolves to here, whether the bootstrap is installed, where
  machine-local state and the memory store live, and which skills shipped
  versus which this session can actually invoke. Disk facts come from a
  helper script; the context facts only the running session can answer, and
  a disagreement between the two columns is the diagnosis.
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
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py"
```

`${CLAUDE_PLUGIN_ROOT}` is the Claude Code spelling. Codex exports
`$PLUGIN_ROOT` and Cursor `$CURSOR_PLUGIN_ROOT`. On Hermes and OpenCode no
plugin-root variable exists at all — but the policy already in your context had
its placeholders substituted before injection, so the absolute path appears in
its machine-local state paragraph. Read it from there. Being unable to locate
the payload at all is itself the first finding: the harness is not looking where
the plugin was installed.

Add `--json` when you want the same facts as data.

## Then answer the three it cannot

A script can prove the hook is installed and that the policy renders. It cannot
prove the policy arrived. Only you can see your own context.

1. **Did the policy load?** Look for the policy wrapper in your context, and
   check that the mapping following it names *this* harness. A policy present
   but carrying another harness's mapping is worse than none, because routing
   then points at models that do not exist here.
2. **Which skills are actually invocable?** Compare your own skill list against
   the script's shipped roster. Mind the naming rule: most harnesses namespace
   them as `leo:<name>`, while OpenCode registers the directory by path and
   names each skill from its own frontmatter, so they appear bare there. A
   skill that looks missing on OpenCode may simply be listed without a prefix.
3. **Is memory present and delivered?** The script reports whether the store
   exists and whether each native surface received its generated copy. Whether
   those facts are in front of you right now is something only you can confirm.
   Report the two separately; they disagree more often than expected.

## Reading the report

Every row carries its source — `env`, `disk`, `config`, or `context` — so a
reader can tell a fact from an inference. Close with one verdict from exactly
three: **healthy**, **degraded**, or **not loaded**. Never free prose. `not
loaded` outranks everything else: if the policy did not arrive, nothing else in
the report describes how this session will actually behave.

**Breadcrumb logs are history, not a verdict.** They carry no timestamps, and
the test suite drives the failure paths deliberately, so entries accumulate on
any machine where the tests have ever run. Quote the newest line if it is
useful, but never conclude "the hook failed this session" from it.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Policy absent, bootstrap installed | the hook fired and failed open | read the newest breadcrumb, then confirm it describes this session before believing it |
| Policy present, mapping names another harness | detection resolved wrong, usually a stray plugin-root variable exported in an unrelated shell | unset it, restart the session |
| Shipped roster exceeds what you can invoke | the harness cached an older payload, or the skills directory is not registered | update the plugin; on OpenCode add the documented skills-path fallback |
| Skills listed without the `leo:` prefix | OpenCode, working as designed | invoke them bare; not a fault |
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
