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


def diagnose(harness, root=None):
    root = Path(root) if root else payload.plugin_root()
    report = {"harness": harness, "root": str(root), "capabilities": CAPABILITIES[harness],
              "runtime_activation": "Not established by disk checks; inspect the running harness's plugin/hook diagnostics.",
              "issues": []}
    try:
        config = routing.load()
        report["tiers"] = {tier: pricing.resolve(routing.tier_model(harness, tier, config) or "unknown").report()
                           for tier in ("cheap", "standard")}
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
    catalog = pricing.load()
    stamp = catalog.get("fetched_at")
    report["pricing"] = {"source": catalog.get("source"), "fetched_at": stamp,
                         "age_hours": round((time.time() - stamp) / 3600, 1) if isinstance(stamp, (int, float)) else None,
                         "models": len(catalog["models"])}
    try:
        report["pricing"]["last_refresh"] = json.loads(pricing.cache_path().with_suffix(".status.json").read_text())
    except (OSError, ValueError):
        report["pricing"]["last_refresh"] = "no local refresh diagnostic"
    if harness == "claude":
        for tier in ("cheap", "standard"):
            model = (report.get("tiers", {}).get(tier) or {}).get("requested")
            if model and model not in ("haiku", "sonnet", "opus", "fable"):
                report["issues"].append(f"Claude {tier} model must be an Agent alias: haiku, sonnet, opus, or fable")
        report["forced_subagent_model"] = (os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL")
                                             if os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE") == "1" else None)
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
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = diagnose(args.harness)
    print(json.dumps(report, indent=2))
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
