#!/usr/bin/env python3
"""Read-only installation, routing, and price diagnostics. Never calls a model."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import payload
import pricing
import routing
from routing_engine import CAPABILITIES
import settings_probe

# The age past which a catalog is called stale: the refresher retries daily,
# so a week means refreshes have been failing or are switched off.
STALE_AFTER_HOURS = 7 * 24


def price_freshness(catalog):
    """Which catalog priced this machine, and its age by its own `fetched_at`.
    The two differ in meaning: a local cache is dated by its last successful
    refresh, the bundled snapshot by the fetch it was built from, so the
    report names which one the age describes. A missing, non-numeric, or
    future stamp yields no age rather than a misleading one. Hand amendments
    after the fetch, when the catalog records them, are reported beside it."""
    try:
        cached = json.loads(pricing.cache_path().read_text()) == catalog
    except (OSError, ValueError):
        cached = False
    stamp = catalog.get("fetched_at")
    result = {"source": catalog.get("source"), "catalog": "local refresh cache" if cached else "bundled snapshot",
              "fetched_at": stamp, "fetched_at_utc": None, "age_hours": None, "stale": None,
              "models": len(catalog.get("models") or [])}
    # Optional: rows a maintainer added or changed by hand after the fetch.
    amended, ids = _past_hours(catalog.get("amended_at")), catalog.get("amended_models")
    if amended is not None:
        count = len(ids) if isinstance(ids, list) else None
        result["amended"] = "amended at %s for %s models" % (_utc(catalog["amended_at"]), "unknown" if count is None else count)
        result.update(amended_at_utc=_utc(catalog["amended_at"]), amended_age_hours=amended, amended_models=count)
    age = _past_hours(stamp)
    if age is None:
        result["note"] = "no usable fetch time recorded (missing, not a number, or in the future); freshness unknown"
        return result
    result.update(fetched_at_utc=_utc(stamp), age_hours=age, stale=age > STALE_AFTER_HOURS)
    if not cached:
        result["note"] = "bundled snapshot shipped with the plugin; a successful refresh replaces it with a dated local cache"
    return result


def _past_hours(stamp):
    """Hours since an epoch stamp, or None for anything that is not a real
    past time (allowing an hour of clock skew)."""
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp != stamp:
        return None
    age = (time.time() - stamp) / 3600
    return None if age < -1 or age == float("inf") else round(max(age, 0), 1)


def _utc(stamp):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
    except (OverflowError, OSError, ValueError):
        return None


# LEOS_AGENT_DISPATCH_GUARD: the guard's spellings for each mode. Anything else
# keeps the guard on and is worth fixing, since it was probably meant as off.
GUARD_ON = ("", "on", "1", "true", "yes", "enable", "enabled")
GUARD_OFF = ("off", "0", "false", "no", "disable", "disabled")


def guard_mode(loaded, issues):
    found = settings_probe.setting("LEOS_AGENT_DISPATCH_GUARD", loaded)
    value = (found["value"] or "").strip().lower()
    mode = "warn" if value == "warn" else "off" if value in GUARD_OFF else "on"
    result = {"mode": mode, "set_in": found["set_in"],
              "value": settings_probe.clean(found["value"], 40) if found["value"] is not None else None,
              "recognised": value in GUARD_ON + GUARD_OFF + ("warn",)}
    if not result["recognised"]:
        issues.append("LEOS_AGENT_DISPATCH_GUARD=%r is not a recognised mode, so the guard stays on; use on, warn, "
                      "or off" % result["value"])
    return result


def subagent_model(loaded, issues):
    """CLAUDE_CODE_SUBAGENT_MODEL is a default under the per-dispatch model and
    agent frontmatter; with CLAUDE_CODE_SUBAGENT_MODEL_FORCE on it overrides
    both, or pins every subagent to the main model when no model is named."""
    model = settings_probe.setting("CLAUDE_CODE_SUBAGENT_MODEL", loaded)
    force = settings_probe.setting("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", loaded)
    named = model["value"] if model["value"] and model["value"].strip().lower() != "inherit" else None
    forced = settings_probe.truthy(force["value"])
    result = {"model": settings_probe.clean(named) if named else None, "model_set_in": model["set_in"],
              "force": forced, "force_set_in": force["set_in"],
              "forced_model": (settings_probe.clean(named) if named else "inherit") if forced else None,
              "precedence": "per-dispatch model, then agent frontmatter, then CLAUDE_CODE_SUBAGENT_MODEL, then the "
                            "main model; CLAUDE_CODE_SUBAGENT_MODEL_FORCE on overrides all of them"}
    if forced:
        target = "the %s model" % result["model"] if named else "the main conversation's model"
        issues.append("CLAUDE_CODE_SUBAGENT_MODEL_FORCE is on: every subagent runs on %s and the tier frontmatter "
                      "and per-dispatch model are ignored; unset it to restore tier routing" % target)
        if force["value"].strip() != "1":
            result["note"] = "the dispatch guard recognises only the value 1, so it treats this setting as off"
    elif named:
        result["note"] = ("default only: leos-agent tiers set frontmatter models and keep them; it applies to agents "
                          "with neither. Claude Code before v2.1.251 let it override frontmatter and dispatch models.")
    return result


def advisor(loaded):
    """Claude Code's advisor: a stronger model the main model consults at
    decision points. Opt-in; reported, never changed here."""
    model = settings_probe.setting("advisorModel", loaded, env_key=False)
    value = model["value"]
    if value is not None and not isinstance(value, str):
        value = None
    disabled = settings_probe.truthy(settings_probe.setting("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", loaded)["value"])
    blockers = [name for name in settings_probe.FLAG_FETCH_ANY_VALUE
                if (settings_probe.setting(name, loaded)["value"] or "").strip()]
    blockers += [name for name in settings_probe.FLAG_FETCH_WHEN_ON + settings_probe.THIRD_PARTY_PROVIDERS
                 if settings_probe.truthy(settings_probe.setting(name, loaded)["value"])]
    if disabled:
        status = "disabled by CLAUDE_CODE_DISABLE_ADVISOR_TOOL"
    elif not value:
        status = "off"
    elif blockers:
        status = "configured but unavailable: " + ", ".join(blockers)
    else:
        status = "configured"
    result = {"model": settings_probe.clean(value) if value else None, "set_in": model["set_in"], "status": status,
              "dispatch_guard": "not involved: the advisor is a server-side tool with no hook-matcher name, so "
                                "PreToolUse never fires for it and the guard neither sees nor routes it"}
    if value:
        result["requires"] = ("an advisor ranked at or above the session's main model, the Anthropic API, and "
                              "feature-flag fetching; a session --advisor flag overrides this setting")
        if (settings_probe.setting("ANTHROPIC_BASE_URL", loaded)["value"] or "").strip():
            result["gateway"] = "ANTHROPIC_BASE_URL is set; the advisor works only if the gateway forwards it intact"
    return result


def routing_config(report):
    """Validate routing.json read-only, section by section, with routing.py's
    own rules. A section that fails is not applied, so the tiers reported
    for it are the defaults; the file itself is left exactly as written."""
    result = {"path": routing.config_path(), "exists": os.path.exists(routing.config_path()),
              "valid": True, "ignored": []}
    report["routing_config"] = result
    try:
        raw = routing.read_raw()
        if not isinstance(raw, dict):
            routing.validate(raw)
    except ValueError as exc:
        result.update(valid=False, ignored=["the whole file"], errors=[str(exc)])
        report["issues"].append("routing.json is invalid and not applied; tiers fall back to defaults: " + str(exc))
        return {}
    config, errors = {}, []
    for harness, entry in raw.items():
        try:
            config.update(routing.validate({harness: entry}))
        except ValueError as exc:
            result["ignored"].append(settings_probe.clean(harness, 40))
            errors.append(str(exc))
    if errors:
        result.update(valid=False, errors=errors)
        report["issues"].append("routing.json has invalid sections that are not applied (%s); those tiers fall back "
                                "to defaults: %s" % (", ".join(result["ignored"]), "; ".join(errors)))
    return config


def diagnose(harness, root=None):
    root = Path(root) if root else payload.plugin_root()
    report = {"harness": harness, "root": str(root), "capabilities": CAPABILITIES[harness],
              "runtime_activation": "Not established by disk checks; inspect the running harness's plugin/hook diagnostics.",
              "issues": []}
    config = routing_config(report)
    try:
        report["tiers"] = {tier: pricing.resolve(routing.tier_model(harness, tier, config) or "unknown").report()
                           for tier in ("cheap", "standard", "premium")}
        report["parent_tier"] = "current parent, determined at dispatch"
        report["policy_bytes"] = len(payload.payload_body(root, harness, config).encode())
        if harness == "cursor":
            import measure_context
            report["policy_bytes"] = len(measure_context.frontmatter(root / "rules/preferences.md")[1].strip().encode())
    except (ValueError, OSError) as exc:
        report["issues"].append("routing/policy: " + str(exc))
    try:
        check = subprocess.run([sys.executable, str(root / "scripts/leo-install.py"), harness, "--check"],
                               capture_output=True, text=True, timeout=20,
                               env={**os.environ, "LEOS_AGENT_ROOT": str(root), "LEOS_AGENT_HARNESS": harness,
                                    "LEOS_AGENT_PRICE_REFRESH": "off", "PYTHONDONTWRITEBYTECODE": "1"})
        report["installation_current"] = check.returncode == 0
        report["installation_report"] = (check.stdout + check.stderr).strip()
        if check.returncode:
            report["issues"].append("installed files differ or could not be checked")
    except (OSError, subprocess.SubprocessError) as exc:
        report["issues"].append("installation check: " + str(exc))
    report["pricing"] = price_freshness(pricing.load())
    try:
        report["pricing"]["last_refresh"] = json.loads(pricing.cache_path().with_suffix(".status.json").read_text())
    except (OSError, ValueError):
        report["pricing"]["last_refresh"] = "no local refresh diagnostic"
    report["output_compression"] = settings_probe.output_compressors(harness)
    # Claude Code applies settings `env` to the hook process; elsewhere the
    # guard sees only the environment the harness was started with.
    loaded, problems = (settings_probe.load_settings(settings_probe.claude_settings_files())
                        if harness == "claude" else ([], []))
    report["dispatch_guard"] = guard_mode(loaded, report["issues"])
    if harness == "claude":
        for tier in ("cheap", "standard", "premium"):
            model = (report.get("tiers", {}).get(tier) or {}).get("requested")
            if model and model not in ("haiku", "sonnet", "opus", "fable"):
                report["issues"].append(f"Claude {tier} model must be an Agent alias: haiku, sonnet, opus, or fable")
        report["claude_settings"] = {"read": sorted({path for _, path, _ in loaded}), "problems": problems,
                                     "not_read": "server-managed, MDM, and --settings sources"}
        report["subagent_model"] = subagent_model(loaded, report["issues"])
        report["forced_subagent_model"] = report["subagent_model"]["forced_model"]
        report["advisor"] = advisor(loaded)
    if harness == "codex":
        report["hook_trust"] = "Review current definitions in /hooks; this tool does not approve them."
    if harness in ("hermes", "pi"):
        report["routing_limit"] = "No native per-dispatch model field; configured tier labels do not establish model selection."
    if harness == "hermes":
        report["tiers"] = {"status": "unsupported", "saved_mappings_applied": False}
        report["native_delegation_setting"] = "Preserved; not managed by this installer."
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=routing.HARNESSES, required=True)
    # Output is always JSON; the flag stays accepted so a skill copy from an
    # earlier release keeps working rather than failing on an unknown argument.
    parser.add_argument("--json", action="store_true", help="accepted for compatibility; output is always JSON")
    args = parser.parse_args()
    report = diagnose(args.harness)
    print(json.dumps(report, indent=2))
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
