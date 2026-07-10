#!/usr/bin/env python3
"""Render the policy-owned portions of host fragments from core/policy/policy-data.json.

The host files still carry host-specific hooks and explanatory `$doc` fields, but their permission
surfaces are generated here. Use ``--check`` in CI/review and ``--write`` only when intentionally
changing the single policy source. Package-manager lifecycle commands are deliberately absent.
"""

import argparse
import copy
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
POLICY = os.path.join(ROOT, "core", "policy", "policy-data.json")
TARGETS = {
    "claude": os.path.join(ROOT, "tools", "claude", "settings-fragment.json"),
    "opencode": os.path.join(ROOT, "tools", "opencode", "opencode-fragment.json"),
    "cursor": os.path.join(ROOT, "tools", "cursor", "permissions-fragment.json"),
    "codex": os.path.join(ROOT, "tools", "codex", "command-policy-notes.json"),
}


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def expected(policy):
    patterns = policy["secretReadPatterns"]
    commands = policy["commandAllow"]["universal"]
    return {
        "claude": {"permissions": {
            "deny": [f"Read({p})" for p in patterns],
            "allow": [f"Bash({c}:*)" for c in commands],
        }},
        "opencode": {"permission": {
            "read": {p: "deny" for p in patterns},
            "bash": {f"{c} *": "allow" for c in commands},
        }},
        "cursor": {"permissions": {
            "deny": [f"Read({p})" for p in patterns],
            "allow": [f"Shell({c})" for c in commands],
        }},
        "codex": {"allowGuidance": {
            "universal": commands,
            "pnpm": [], "yarn": [], "npm": [],
        }, "secretReadGuidance": patterns},
    }


def replace_policy_fields(host, doc, want):
    out = copy.deepcopy(doc)
    if host == "opencode":
        out["permission"] = want["permission"]
    elif host == "codex":
        out["allowGuidance"] = want["allowGuidance"]
        out["secretReadGuidance"] = want["secretReadGuidance"]
    else:
        out["permissions"] = want["permissions"]
    return out


def main():
    ap = argparse.ArgumentParser(prog="leos-render-policy.py")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = ap.parse_args()
    try:
        rendered = expected(load(POLICY))
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as e:
        print(f"invalid policy source: {e}", file=sys.stderr)
        return 2
    drift = []
    for host, target in TARGETS.items():
        current = load(target)
        updated = replace_policy_fields(host, current, rendered[host])
        if current != updated:
            drift.append(host)
            if args.write:
                with open(target, "w", encoding="utf-8") as f:
                    json.dump(updated, f, indent=2)
                    f.write("\n")
    print(json.dumps({"ok": not drift, "drift": drift, "mode": "write" if args.write else "check"}, indent=2))
    return 0 if not drift or args.write else 1


if __name__ == "__main__":
    sys.exit(main())
