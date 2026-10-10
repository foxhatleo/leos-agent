#!/usr/bin/env python3
"""Read-only installation, routing, and price diagnostics. Never calls a model."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_guard
import payload
import pricing
import review_peer
import routing
import routing_engine
from routing_engine import CAPABILITIES
import settings_probe
import state

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


def guard_mode(loaded, issues):
    """LEOS_AGENT_DISPATCH_GUARD, read with the guard's own spellings. Any
    other value keeps the guard on and is worth fixing, since it was probably
    meant as off."""
    found = settings_probe.setting("LEOS_AGENT_DISPATCH_GUARD", loaded)
    value = (found["value"] or "").strip().lower()
    mode = "off" if value in dispatch_guard.GUARD_OFF else "warn" if value == "warn" else "on"
    result = {"mode": mode, "set_in": found["set_in"],
              "value": settings_probe.clean(found["value"], 40) if found["value"] is not None else None,
              "recognised": mode != "on" or value in dispatch_guard.GUARD_ON}
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
    # The guard reads the flag the way Claude Code does, so one verdict covers both.
    forced = (force["value"] or "").strip().lower() in dispatch_guard.CLAUDE_TRUE
    result = {"model": settings_probe.clean(named) if named else None, "model_set_in": model["set_in"],
              "force": forced, "force_set_in": force["set_in"],
              "forced_model": (settings_probe.clean(named) if named else "inherit") if forced else None,
              "precedence": "per-dispatch model, then agent frontmatter, then CLAUDE_CODE_SUBAGENT_MODEL, then the "
                            "main model; CLAUDE_CODE_SUBAGENT_MODEL_FORCE on overrides all of them"}
    if forced:
        target = "the %s model" % result["model"] if named else "the main conversation's model"
        issues.append("CLAUDE_CODE_SUBAGENT_MODEL_FORCE is on: every subagent runs on %s and the tier frontmatter "
                      "and per-dispatch model are ignored; unset it to restore tier routing" % target)
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
    """routing.json, read-only, through the loader the guard and the session
    hook use, so the config reported is the one they apply. A section that
    loader drops is reported ignored and its tiers are the defaults; the file
    itself is left exactly as written."""
    result = {"path": routing.config_path(), "exists": os.path.exists(routing.config_path()),
              "valid": True, "ignored": []}
    report["routing_config"] = result
    config, error = routing_engine.load_config()
    if error is None:
        return config
    try:
        raw = routing.read_raw()
    except (ValueError, OSError):
        raw = None
    if not isinstance(raw, dict):
        result.update(valid=False, ignored=["the whole file"], errors=[str(error)])
        report["issues"].append("routing.json is invalid and not applied; tiers fall back to defaults: " + str(error))
        return config
    # An empty section applies nothing and is valid. Any other section the
    # loader left out failed on its own, and validating it alone says why.
    errors = []
    for harness, entry in raw.items():
        if harness in config or entry == {}:
            continue
        result["ignored"].append(settings_probe.clean(harness, 40))
        try:
            routing.validate({harness: entry})
        except ValueError as exc:
            errors.append(str(exc))
    result.update(valid=False, errors=errors or [str(error)])
    report["issues"].append("routing.json has invalid sections that are not applied (%s); those tiers fall back "
                            "to defaults: %s" % (", ".join(result["ignored"]), "; ".join(result["errors"])))
    return config


def review_peer_setting(harness, issues):
    """The opt-in cross-model review lens on this machine, read-only.

    Only the keys review_peer.py reads are reported, never the rest of the
    file. The peer CLI is looked up on PATH and not run: its login is checked
    when a review runs the lens (`review_peer.py detect`)."""
    path = os.path.join(state._data_root(), review_peer.SETTINGS_NAME + ".json")
    result = {"path": path, "exists": os.path.isfile(path), "enabled": False, "peer": None,
              "peer_source": None, "peer_cli": None, "peer_cli_detected": None}
    stored = {}
    if result["exists"]:
        try:
            stored = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = None
        if not isinstance(stored, dict):
            result["status"] = "unreadable"
            issues.append("review-peer.json is not a readable JSON object, so review_peer.py refuses to run; fix it, "
                          "or delete it to turn the cross-model lens off")
            return result
    settings = dict(review_peer.DEFAULTS)
    settings.update((k, v) for k, v in stored.items() if k in review_peer.DEFAULTS)
    # review_peer.plan's reading: the configured target, else the other
    # family of a Claude or Codex host; only `true` enables it.
    host = harness if harness in review_peer.OTHER else None
    target = settings["target"] or review_peer.OTHER.get(host)
    enabled = settings["enabled"] is True
    result["enabled"] = enabled
    off = "off: review-pr runs the lens only when you enable it for one review or with review_peer.py config --enable"
    if not isinstance(target, str) or target not in review_peer.TARGETS:
        result["status"] = off if not enabled else ("enabled, but no peer for this harness; choose one with "
                                                    "review_peer.py config --target codex or claude")
        return result
    binary = review_peer.TARGETS[target]["binary"]
    result.update(peer=target, peer_source="setting" if settings["target"] else "default for this harness",
                  peer_cli=binary, peer_cli_detected=shutil.which(binary) is not None)
    if isinstance(settings["model"], str):
        result["model"] = settings_probe.clean(settings["model"], 100)
    if not enabled:
        result["status"] = off
    elif host == review_peer.TARGETS[target]["family"]:
        result["status"] = "enabled, but the peer is this harness's own model family, so review-pr skips the lens here"
    elif not result["peer_cli_detected"]:
        result["status"] = "enabled, but %s is not on PATH, so review-pr skips the lens" % binary
        issues.append("the cross-model review lens is enabled but %s is not on PATH; install it, or run "
                      "review_peer.py config --disable" % binary)
    else:
        result["status"] = ("enabled: a review checks that %s is logged in, then sends the pinned PR diff to %s's "
                            "provider" % (binary, target))
    return result


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
    report["review_peer"] = review_peer_setting(harness, report["issues"])
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
