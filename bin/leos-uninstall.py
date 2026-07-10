#!/usr/bin/env python3
"""Ownership-safe removal of one configured Leo host integration."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOCAL = Path(os.environ.get("LEOS_LOCAL", ROOT / "local"))
HOSTS = ("claude", "codex", "opencode", "cursor")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def expand(value):
    codex_home = os.environ.get("CODEX_HOME", os.path.join(os.path.expanduser("~"), ".codex"))
    return os.path.abspath(os.path.expanduser(value.replace("{{CODEX_HOME}}", codex_home)))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".uninstall-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, sort_keys=True); f.write("\n")
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def main():
    ap = argparse.ArgumentParser(prog="leos-uninstall.py")
    ap.add_argument("--tool", required=True, choices=HOSTS)
    args = ap.parse_args()
    registry_path = LOCAL / "installed-hosts.json"
    registry = load_json(registry_path, {"hosts": []})
    configured = {host for host in registry.get("hosts", []) if host in HOSTS}
    remaining = configured - {args.tool}
    shared = set()
    for host in remaining:
        lm = load_json(ROOT / "tools" / host / "linkmap.json", {})
        shared.update(expand(item["dest"]) for item in lm.get("links", []))

    linkmap = load_json(ROOT / "tools" / args.tool / "linkmap.json", {})
    results = []
    # Migrate/remove legacy whole-file hook links before ownership removal.
    for item in linkmap.get("merges", []):
        dest = expand(item["dest"])
        expected = os.path.realpath(ROOT / item["fragment"])
        if os.path.islink(dest) and os.path.realpath(dest) == expected:
            os.unlink(dest)
            results.append({"dest": dest, "action": "removed-legacy-fragment-link"})

    merge = subprocess.run([str(ROOT / "bin" / "leos-python"), str(ROOT / "bin" / "leos-merge.py"),
                            "--tool", args.tool, "--remove"], capture_output=True, text=True)
    if merge.returncode != 0:
        print(json.dumps({"ok": False, "stage": "merge-removal", "details": merge.stdout or merge.stderr}, indent=2))
        return 1
    if linkmap.get("blocks"):
        block = subprocess.run([str(ROOT / "bin" / "leos-python"), str(ROOT / "bin" / "leos-block.py"),
                                "--tool", args.tool, "--remove"], capture_output=True, text=True)
        if block.returncode != 0:
            print(json.dumps({"ok": False, "stage": "block-removal", "details": block.stdout or block.stderr}, indent=2))
            return 1
    for item in linkmap.get("links", []):
        dest = expand(item["dest"])
        expected = os.path.realpath(ROOT / item["src"])
        if dest in shared:
            results.append({"dest": dest, "action": "kept-shared"})
        elif not os.path.lexists(dest):
            results.append({"dest": dest, "action": "absent"})
        elif os.path.islink(dest) and os.path.realpath(dest) == expected:
            os.unlink(dest)
            results.append({"dest": dest, "action": "removed-link"})
        else:
            print(json.dumps({"ok": False, "stage": "link-removal", "dest": dest,
                              "reason": "destination is no longer Leo's expected symlink"}, indent=2))
            return 1
    try:
        os.unlink(LOCAL / f"seats.{args.tool}.json")
    except FileNotFoundError:
        pass
    registry["hosts"] = sorted(remaining)
    atomic_json(registry_path, registry)
    print(json.dumps({"ok": True, "tool": args.tool, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
