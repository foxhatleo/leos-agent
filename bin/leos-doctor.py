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
  4. instructions — leos's global instructions are delivered ADDITIVELY (Claude @import block,
                   OpenCode instructions[], Codex SessionStart injector) rather than a clobbering
                   symlink; a leftover clone-symlink at a retired delivery path is flagged.

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


def _read_text(path):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def check_instructions_delivery(tool, problems, tinfo):
    """Verify leos's global instructions are delivered ADDITIVELY (never as a clobbering symlink):
    Claude via an @import block, OpenCode via instructions[], Codex via the SessionStart injector.
    Cursor has no global instruction file, so nothing to check."""
    gi = os.path.join(REPO_ROOT, "global", "AGENTS.md")
    if tool == "claude":
        txt = _read_text("~/.claude/CLAUDE.md")
        if txt is None or "leos-agent:global-instructions" not in txt:
            problems.append("claude: @import block missing from ~/.claude/CLAUDE.md — run leos-block --tool claude")
            tinfo["instructions"] = "missing"
        elif f"@{gi}" not in txt:
            problems.append(f"claude: @import block does not point at {gi} (moved clone?) — re-run leos-block --tool claude")
            tinfo["instructions"] = "stale"
        else:
            tinfo["instructions"] = "ok"
    elif tool == "opencode":
        try:   # user-hand-editable file: a malformed opencode.json must be reported, not crash doctor
            data = load_json(os.path.expanduser("~/.config/opencode/opencode.json"), None)
        except Exception:
            data = None
        instr = data.get("instructions") if isinstance(data, dict) else None
        if not isinstance(instr, list) or gi not in instr:
            problems.append(f"opencode: instructions[] does not include {gi} — re-run leos-merge --tool opencode")
            tinfo["instructions"] = "missing"
        else:
            tinfo["instructions"] = "ok"
    elif tool == "codex":
        txt = _read_text("~/.codex/hooks.json")
        if txt is None or "SessionStart" not in txt or "inject-instructions.py" not in txt:
            problems.append("codex: SessionStart instruction injector not registered in ~/.codex/hooks.json")
            tinfo["instructions"] = "missing"
        else:
            tinfo["instructions"] = "ok"


def check_tool(tool, problems, report):
    lm = load_json(os.path.join(REPO_ROOT, "tools", tool, "linkmap.json"), {})
    home = os.path.expanduser(lm.get("toolHome", ""))
    if not home or not os.path.isdir(home):
        report.append({"tool": tool, "installed": False})
        return
    tinfo = {"tool": tool, "installed": True, "links": [], "merges": [], "seats": None,
             "instructions": None}

    for e in lm.get("links", []):
        dest = os.path.expanduser(e["dest"])
        src = os.path.join(REPO_ROOT, e["src"])
        st = link_state(dest, src)
        if st != "linked":
            problems.append(f"{tool}: link {e['dest']} is '{st}' (want linked)")
        tinfo["links"].append({"dest": e["dest"], "state": st})

    # Retired symlinks: a leftover clone-symlink at an old delivery path masks the user's own
    # global file — flag it (read-only; the user removes it).
    for e in lm.get("retiredLinks", []):
        dest = os.path.expanduser(e["dest"])
        if os.path.islink(dest):
            target = os.path.realpath(dest)
            if target == REPO_ROOT or target.startswith(REPO_ROOT + os.sep):
                problems.append(f"{tool}: leftover clone-symlink at {e['dest']} — remove it "
                                f"(delivery is now additive; the symlink masks your own global file)")

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

    check_instructions_delivery(tool, problems, tinfo)
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
