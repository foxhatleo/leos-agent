#!/usr/bin/env python3
"""leos-block — ensure a managed `@import` block inside a host-owned instruction file.

Claude Code resolves `@import` natively, so leos-agent's global instructions are delivered to Claude
as a one-line reference — `@<clone>/global/AGENTS.md` — inside a marker-delimited block appended to
`~/.claude/CLAUDE.md`. This COEXISTS with the user's own CLAUDE.md content (never clobbers it), and
`git pull` upgrades the imported file live (the block only rewrites if the clone path changes).

Reads the `blocks` array of tools/<tool>/linkmap.json. Idempotent. Backs the dest up before any
rewrite. HOME-guarded (refuses a dest outside $HOME). Handles four dest states: missing (create),
real file (marker-scoped replace, else append), a legacy bare symlink INTO the clone (migrate to a
real file with the block), and a foreign symlink (follow it, ensure the block in the real target).

Usage:
  leos-block.py --tool claude            # create/repair the block(s)
  leos-block.py --tool claude --check    # verify only (exit 1 on problems)

Stdlib only.
"""

import argparse
import json
import os
import shutil
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LOCAL = os.environ.get("LEOS_LOCAL", os.path.join(REPO_ROOT, "local"))  # override for tests
HOME = os.path.realpath(os.path.expanduser("~"))


def _resolve(dest):
    """Absolute dest with PARENT symlinks resolved but the FINAL component NOT followed — so a
    symlink AT the dest is detectable (realpath would follow it and we'd edit its target, e.g. the
    committed global/AGENTS.md). REFUSES anything outside $HOME."""
    p = os.path.expanduser(dest)
    path = os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))
    if not (path == HOME or path.startswith(HOME + os.sep)):
        raise SystemExit(f"refusing dest outside HOME: {dest}")
    return path


def block_text(marker, import_abs):
    return f"<!-- {marker} BEGIN -->\n@{import_abs}\n<!-- {marker} END -->"


def ensure_in_text(text, marker, import_abs):
    """Return `text` with the managed block created/refreshed. Idempotent."""
    begin = f"<!-- {marker} BEGIN -->"
    end = f"<!-- {marker} END -->"
    blk = block_text(marker, import_abs)
    if begin in text and end in text:
        i = text.index(begin)
        j = text.index(end) + len(end)
        return text[:i] + blk + text[j:]
    if text.strip() == "":
        return blk + "\n"
    return text.rstrip("\n") + "\n\n" + blk + "\n"


def _backup(path):
    if not os.path.exists(path):
        return None
    bdir = os.path.join(LOCAL, "backups", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bdir, exist_ok=True)
    target = os.path.join(bdir, os.path.basename(path) + ".claude-md")
    shutil.copy2(path, target)
    return target


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _ensure_realfile(path, marker, import_abs, do_write, label):
    try:
        with open(path, encoding="utf-8") as f:
            cur = f.read()
    except OSError as e:
        return {"dest": label, "ok": False, "state": f"unreadable: {e}"}
    new = ensure_in_text(cur, marker, import_abs)
    if new == cur:
        return {"dest": label, "ok": True, "action": "ok"}
    if not do_write:
        return {"dest": label, "ok": False, "state": "block-absent-or-stale"}
    _backup(path)
    _write(path, new)
    return {"dest": label, "ok": True, "action": "updated"}


def ensure_block(dest_key, marker, import_abs, do_write):
    dest = _resolve(dest_key)
    if os.path.islink(dest):
        target = os.path.realpath(dest)
        if target == REPO_ROOT or target.startswith(REPO_ROOT + os.sep):
            # legacy bare symlink INTO the clone -> migrate to a real file carrying the block
            if not do_write:
                return {"dest": dest_key, "ok": False, "state": "legacy-symlink-into-clone"}
            _backup(dest)
            os.remove(dest)
            _write(dest, ensure_in_text("", marker, import_abs))
            return {"dest": dest_key, "ok": True, "action": "migrated-from-symlink"}
        # foreign symlink -> ensure the block in the real target, non-destructively — but only if
        # that target is itself under $HOME (the module's write boundary); refuse otherwise so we
        # never write through a symlink to a file outside HOME.
        if not (target == HOME or target.startswith(HOME + os.sep)):
            return {"dest": dest_key, "ok": False, "state": f"symlink target outside HOME: {target}"}
        return _ensure_realfile(target, marker, import_abs, do_write, dest_key)
    if not os.path.exists(dest):
        if not do_write:
            return {"dest": dest_key, "ok": False, "state": "missing"}
        _write(dest, ensure_in_text("", marker, import_abs))
        return {"dest": dest_key, "ok": True, "action": "created"}
    return _ensure_realfile(dest, marker, import_abs, do_write, dest_key)


def blocks_for(tool):
    with open(os.path.join(REPO_ROOT, "tools", tool, "linkmap.json")) as f:
        lm = json.load(f)
    return lm.get("blocks", [])


def main():
    ap = argparse.ArgumentParser(prog="leos-block.py")
    ap.add_argument("--tool", required=True, choices=["claude", "codex", "opencode", "cursor"])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    results, problems = [], False
    for b in blocks_for(args.tool):
        import_abs = os.path.join(REPO_ROOT, b["import"])
        r = ensure_block(b["dest"], b["marker"], import_abs, not args.check)
        if not r.get("ok"):
            problems = True
        results.append(r)
    print(json.dumps(results, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
