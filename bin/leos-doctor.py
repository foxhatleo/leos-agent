#!/usr/bin/env python3
"""leos-doctor — health check for an installed leos-agent (no version numbers).

Three checks, all read-only:
  1. linkcheck   — every symlink in each installed tool's linkmap is present, points into the
                   clone, and is still a symlink (a host that rewrote a whole-file symlink into a
                   real file is caught here, not silently).
  2. fragment-drift — the ONE thing `git pull` cannot auto-apply: a committed merge fragment
                   (settings/config/opencode/cli-config) that changed since it was last merged.
                   Reported as "re-run leos-merge --tool X".
  3. seat-flags  — each machine-local local/seats.<host>.json carries its recursion-isolation flag
                   (claude --safe-mode, codex --sandbox read-only, opencode --agent plan).

A tool is only checked if its home dir exists (i.e. it's installed on this machine).
Exit 1 if any problem is found. Stdlib only.
"""

import hashlib
import json
import os
import sys
import tomllib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TOOLS = ["claude", "codex", "opencode", "cursor"]


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _strip_doc(d):
    return {k: v for k, v in d.items() if not str(k).startswith("$")}


def frag_sha(path, strategy):
    try:
        if strategy == "merge-toml":
            with open(path, "rb") as f:
                data = tomllib.load(f)
        else:
            data = load_json(path, None)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    text = json.dumps(_strip_doc(data), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def link_state(dest, src):
    if not os.path.islink(dest) and not os.path.exists(dest):
        return "missing"
    if os.path.islink(dest):
        target = os.readlink(dest)
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(dest), target)
        if os.path.normpath(target) == os.path.normpath(src):
            return "linked" if os.path.exists(src) else "dangling"
        return "wrong-link"
    return "foreign"


def check_tool(tool, problems, report):
    lm = load_json(os.path.join(REPO_ROOT, "tools", tool, "linkmap.json"), {})
    home = os.path.expanduser(lm.get("toolHome", ""))
    if not home or not os.path.isdir(home):
        report.append({"tool": tool, "installed": False})
        return
    tinfo = {"tool": tool, "installed": True, "links": [], "merges": [], "seats": None}

    for e in lm.get("links", []):
        dest = os.path.expanduser(e["dest"])
        src = os.path.join(REPO_ROOT, e["src"])
        st = link_state(dest, src)
        if st != "linked":
            problems.append(f"{tool}: link {e['dest']} is '{st}' (want linked)")
        tinfo["links"].append({"dest": e["dest"], "state": st})

    state = load_json(os.path.join(REPO_ROOT, "local", "merge-state.json"), {"merges": {}})
    for m in lm.get("merges", []):
        cur = frag_sha(os.path.join(REPO_ROOT, m["fragment"]), m["strategy"])
        rec = state["merges"].get(m["dest"], {}).get("fragmentSha")
        if rec is None:
            drift = "never-merged"
            problems.append(f"{tool}: fragment {m['fragment']} never merged — run leos-merge --tool {tool}")
        elif rec != cur:
            drift = "changed"
            problems.append(f"{tool}: fragment {m['fragment']} changed since last merge — re-run leos-merge --tool {tool}")
        else:
            drift = "current"
        tinfo["merges"].append({"dest": m["dest"], "drift": drift})

    seats = load_json(os.path.join(REPO_ROOT, "local", f"seats.{tool}.json"), None)
    if seats is not None:
        seat_problems = check_seat_flags(tool, seats)
        problems.extend(f"{tool}: {p}" for p in seat_problems)
        tinfo["seats"] = "ok" if not seat_problems else "problems"
    report.append(tinfo)


def _argv_of(seat):
    return [str(x) for x in seat.get("argv", [])]


def check_seat_flags(tool, seats):
    problems = []
    all_seats = list(seats.get("seats", []))
    native = seats.get("native") or {}
    if native.get("mode") == "exec":
        all_seats = all_seats + [native]
    for seat in all_seats:
        argv = _argv_of(seat)
        if not argv:
            continue
        base = os.path.basename(argv[0])
        joined = " ".join(argv)
        if base == "claude" and "--safe-mode" not in argv:
            problems.append(f"claude seat missing --safe-mode: {joined}")
        if base == "codex" and not ("read-only" in joined):
            problems.append(f"codex seat not --sandbox read-only: {joined}")
        if base == "opencode" and not ("plan" in argv):
            problems.append(f"opencode seat missing --agent plan: {joined}")
        if base == "cursor-agent" and not ("plan" in joined):
            problems.append(f"cursor-agent seat not --mode plan: {joined}")
    return problems


def main():
    problems, report = [], []
    for tool in TOOLS:
        check_tool(tool, problems, report)
    print(json.dumps({"ok": not problems, "problems": problems, "report": report}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
