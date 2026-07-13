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
import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
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
        j = text.index(end, i) + len(end)   # search from i: an orphan END before BEGIN must not garble the splice
        return text[:i] + blk + text[j:]
    if text.strip() == "":
        return blk + "\n"
    return text.rstrip("\n") + "\n\n" + blk + "\n"


def remove_from_text(text, marker):
    """Remove exactly the managed marker block while preserving all foreign content."""
    begin = f"<!-- {marker} BEGIN -->"
    end = f"<!-- {marker} END -->"
    if begin not in text or end not in text:
        return text
    i = text.index(begin)
    j = text.index(end, i) + len(end)
    before = text[:i].rstrip("\n")
    after = text[j:].lstrip("\n")
    if before and after:
        return before + "\n\n" + after
    if before:
        return before + "\n"
    return after


def _backup(path):
    if not os.path.exists(path):
        return None
    bdir = os.path.join(LOCAL, "backups", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bdir, exist_ok=True, mode=0o700)
    target_base = os.path.basename(path) + ".claude-md"
    target = os.path.join(bdir, target_base)
    n = 1
    while os.path.lexists(target):
        target = os.path.join(bdir, f"{target_base}.{n}")
        n += 1
    shutil.copy2(path, target)
    return target


def _write(path, text):
    """Stage under local/ and atomically replace the final component (including a legacy
    symlink).  Refuse a cross-device write rather than allocating temp data elsewhere."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    staging = os.path.join(LOCAL, "staging")
    os.makedirs(staging, exist_ok=True, mode=0o700)
    if os.stat(staging).st_dev != os.stat(parent).st_dev:
        raise OSError("local staging and destination are on different filesystems; refusing non-atomic write")
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o600
    fd, tmp = tempfile.mkstemp(prefix="block-", dir=staging)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _block_lock():
    """Serialize managed-block inspection/backup/replacement across setup sessions."""
    os.makedirs(LOCAL, exist_ok=True, mode=0o700)
    path = os.path.join(LOCAL, "block.lock")
    with open(path, "a+") as lock:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        try:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


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
        managed_import = os.path.realpath(import_abs)
        if target == managed_import:
            # The known legacy bare symlink to global/AGENTS.md -> migrate to a real file carrying
            # the block. Do not use the broad "anything inside clone" test: a user may keep a
            # dotfile target under clone/local/, and an arbitrary tracked clone target must never
            # be edited through a host symlink.
            if not do_write:
                return {"dest": dest_key, "ok": False, "state": "legacy-symlink-into-clone"}
            _backup(dest)
            _write(dest, ensure_in_text("", marker, import_abs))
            return {"dest": dest_key, "ok": True, "action": "migrated-from-symlink"}
        if target == REPO_ROOT or target.startswith(REPO_ROOT + os.sep):
            local_roots = {os.path.realpath(LOCAL), os.path.realpath(os.path.join(REPO_ROOT, "local"))}
            if not any(target == local_root or target.startswith(local_root + os.sep)
                       for local_root in local_roots):
                return {"dest": dest_key, "ok": False,
                        "state": f"foreign clone symlink target refused: {target}"}
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


def remove_block(dest_key, marker):
    dest = _resolve(dest_key)
    target = os.path.realpath(dest) if os.path.islink(dest) else dest
    if not (target == HOME or target.startswith(HOME + os.sep)):
        return {"dest": dest_key, "ok": False, "state": "target outside HOME"}
    try:
        with open(target, encoding="utf-8") as f:
            current = f.read()
    except FileNotFoundError:
        return {"dest": dest_key, "ok": True, "action": "absent"}
    except OSError as e:
        return {"dest": dest_key, "ok": False, "state": f"unreadable: {e}"}
    updated = remove_from_text(current, marker)
    if updated == current:
        return {"dest": dest_key, "ok": True, "action": "absent"}
    _backup(target)
    _write(target, updated)
    return {"dest": dest_key, "ok": True, "action": "removed"}


def blocks_for(tool):
    with open(os.path.join(REPO_ROOT, "tools", tool, "linkmap.json")) as f:
        lm = json.load(f)
    return lm.get("blocks", [])


def main():
    ap = argparse.ArgumentParser(prog="leos-block.py")
    ap.add_argument("--tool", required=True, choices=["claude", "codex", "opencode", "cursor"])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()

    results, problems = [], False
    with _block_lock():
        for b in blocks_for(args.tool):
            import_abs = os.path.join(REPO_ROOT, b["import"])
            r = remove_block(b["dest"], b["marker"]) if args.remove else \
                ensure_block(b["dest"], b["marker"], import_abs, not args.check)
            if not r.get("ok"):
                problems = True
            results.append(r)
    print(json.dumps(results, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
