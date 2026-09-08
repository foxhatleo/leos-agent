---
name: tune-routing
disable-model-invocation: true
description: Configure cheap/standard model mappings for this harness, inspect price references and native capability, and verify installation. Live model probes require separate authorization and a budget.
argument-hint: "[model preferences]"
---

# Tune leos-agent routing

Configure only the current harness or one the user explicitly named. Resolve the
absolute plugin root from `LEOS_AGENT_ROOT`, `CLAUDE_PLUGIN_ROOT`, `PLUGIN_ROOT`,
or the nearest ancestor containing `rules/preferences.md`.

1. Read current settings and diagnostics:

   ```
   python3 "<plugin-root>/scripts/routing.py" show
   python3 "<plugin-root>/scripts/doctor.py" --harness <harness> --json
   ```

   For Hermes, stop after diagnostics: per-task tiers are unsupported. Explain
   that the delegation-worthwhile policy still applies, preserve its native
   delegation setting, and do not configure ineffective cheap/standard mappings.

2. Identify concrete model IDs available to this account. Read
   `reference/harnesses.md` for the native selection mechanism. Price catalog
   presence does not establish account availability. Ask the user when their
   desired capability/cost tradeoff or available IDs are unclear.
3. Recommend cheap for bounded factual/mechanical work, standard for ordinary
   diagnosis/implementation, and current parent for more demanding work. Keep
   parent-level dynamic. Inspect reference matches without altering IDs:

   ```
   python3 "<plugin-root>/scripts/pricing.py" resolve <model-id>
   ```

   Unknown/ambiguous matches remain allowed with diagnostics; report that
   limitation. Crossover prices do not establish a universal ranking. Check
   immediate-parent ceilings, including a parent already on a cheap model.
4. Write the chosen mapping with the helper, preserving other harness entries:

   ```
   python3 "<plugin-root>/scripts/routing.py" set --harness <harness> \
     --cheap <model-id> --standard <model-id>
   python3 "<plugin-root>/scripts/leo-install.py" <harness>
   ```

   Set only requested roles. Optional `--cheap-effort`/`--standard-effort` must
   be supported by the chosen native model. Legacy runner/executor aliases are
   accepted, but do not specify an alias and its canonical role together.
5. Run installation `--check` and inspect diagnostics. A successful check proves
   current files, not native model availability or execution. Start/reload the
   harness as its native configuration requires. Do not claim routing active
   solely because its stanza rendered.

Live probes are optional and **never automatic**. Obtain authorization and a
spending cap, then use a tiny bounded task and inspect actual model observations.
Include parent orchestration in cost. An invalid model should prompt correction
or restoration of the specific previous mapping; do not erase unrelated config.
CI and ordinary setup checks must never launch paid model work.
