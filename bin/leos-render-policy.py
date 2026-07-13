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
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
POLICY = os.path.join(ROOT, "core", "policy", "policy-data.json")
LOCAL = os.environ.get("LEOS_LOCAL", os.path.join(ROOT, "local"))
TARGETS = {
    "claude": os.path.join(ROOT, "tools", "claude", "settings-fragment.json"),
    "opencode": os.path.join(ROOT, "tools", "opencode", "opencode-fragment.json"),
    "cursor": os.path.join(ROOT, "tools", "cursor", "permissions-fragment.json"),
    "codex": os.path.join(ROOT, "tools", "codex", "command-policy-notes.json"),
}


def load(path):
    """Return the target JSON, or {} if the fragment is absent (fresh/partial checkout) so a
    --check reports drift rather than crashing with an uncaught traceback."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return {}


def atomic_write(path, value):
    """Stage under local/ then atomically replace (mirrors leos-merge._atomic_write) so an
    interrupt mid-write never leaves a truncated committed fragment."""
    staging = os.path.join(LOCAL, "staging")
    os.makedirs(staging, exist_ok=True)
    parent = os.path.dirname(path)
    if os.stat(staging).st_dev != os.stat(parent).st_dev:
        raise OSError("local staging and target are on different filesystems; refusing non-atomic write")
    fd, tmp = tempfile.mkstemp(prefix="policy-", dir=staging)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


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
                atomic_write(target, updated)
    print(json.dumps({"ok": not drift, "drift": drift, "mode": "write" if args.write else "check"}, indent=2))
    return 0 if not drift or args.write else 1


if __name__ == "__main__":
    sys.exit(main())
