#!/usr/bin/env python3
"""leos-link — create/verify the symlink farm from a tool's linkmap.

Symlinks each `links` entry in tools/<tool>/linkmap.json from the tool home into the leos-agent
clone. Uses lstat/readlink semantics (NEVER realpath), so it can't reproduce the old apply.py
failure modes (delete-through-link, escape-refusal). Idempotent. REFUSES to replace a foreign
regular file/dir without --force (the first-link policy). There is no link-following `remove`.

Usage:
  leos-link.py --tool {claude|codex|opencode|cursor} [--force]   # create/repair links
  leos-link.py --tool {..} --check                                # verify only (exit 1 on problems)

Stdlib only.
"""

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def load_linkmap(tool):
    with open(os.path.join(REPO_ROOT, "tools", tool, "linkmap.json")) as f:
        return json.load(f)


def link_targets(tool):
    lm = load_linkmap(tool)
    out = []
    for entry in lm.get("links", []):
        dest = os.path.expanduser(entry["dest"])
        src = os.path.join(REPO_ROOT, entry["src"])
        out.append((dest, src, entry))
    return out


def link_state(dest, src):
    """Classify without following the link: 'linked' (points at src), 'wrong-link'
    (symlink elsewhere), 'foreign' (real file/dir), 'dangling' (link to missing),
    'missing'."""
    if not os.path.islink(dest) and not os.path.exists(dest):
        return "missing"
    if os.path.islink(dest):
        target = os.readlink(dest)
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(dest), target)
        target = os.path.normpath(target)
        if target == os.path.normpath(src):
            return "linked" if os.path.exists(src) else "dangling"
        return "wrong-link"
    return "foreign"  # a real file/dir we did not create


def make_link(dest, src, force):
    st = link_state(dest, src)
    if st == "linked":
        return {"dest": dest, "action": "ok"}
    if st == "foreign" and not force:
        return {"dest": dest, "action": "refused",
                "reason": "existing non-symlink content — pass --force after backing it up"}
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    tmp = dest + ".tmp-leoslink"
    if os.path.islink(tmp) or os.path.exists(tmp):
        os.remove(tmp)
    os.symlink(src, tmp)
    os.replace(tmp, dest)  # atomic: the dest path is never absent mid-swap
    return {"dest": dest, "action": "relinked" if st != "missing" else "linked",
            "was": st}


def main():
    ap = argparse.ArgumentParser(prog="leos-link.py")
    ap.add_argument("--tool", required=True, choices=["claude", "codex", "opencode", "cursor"])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    results = []
    problems = False
    for dest, src, _entry in link_targets(args.tool):
        if args.check:
            st = link_state(dest, src)
            ok = st == "linked"
            if not ok:
                problems = True
            results.append({"dest": dest, "state": st, "ok": ok})
        else:
            r = make_link(dest, src, args.force)
            if r["action"] == "refused":
                problems = True
            results.append(r)
    print(json.dumps(results, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
