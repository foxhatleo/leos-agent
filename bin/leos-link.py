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
import contextlib
import json
import os
import secrets
import shutil
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LOCAL = os.environ.get("LEOS_LOCAL", os.path.join(REPO_ROOT, "local"))
HOME = os.path.realpath(os.path.expanduser("~"))


def _expand_dest(value):
    value = value.replace("{{CODEX_HOME}}", os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex")))
    expanded = os.path.expanduser(value)
    # Resolve parent links but never follow a final symlink: it must remain classifiable below.
    path = os.path.join(os.path.realpath(os.path.dirname(expanded)), os.path.basename(expanded))
    if not (path == HOME or path.startswith(HOME + os.sep)):
        raise SystemExit(f"refusing destination outside HOME: {value}")
    return path


def _secure_dir(path):
    os.makedirs(path, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _record_configured(tool):
    """A host is configured because Leo linked it, not merely because its config directory happens
    to exist.  Doctor consumes this small gitignored registry."""
    _secure_dir(LOCAL)
    path = os.path.join(LOCAL, "installed-hosts.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {"hosts": []}
    hosts = data.get("hosts") if isinstance(data, dict) else []
    hosts = [h for h in hosts if h in ("claude", "codex", "opencode", "cursor")]
    if tool not in hosts:
        hosts.append(tool)
    tmp = os.path.join(LOCAL, "staging", "installed-hosts-" + secrets.token_hex(16))
    _secure_dir(os.path.dirname(tmp))
    with open(tmp, "w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)
        json.dump({"hosts": sorted(hosts)}, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def _link_lock():
    """Serialize link replacement and installed-host registry updates across setup sessions."""
    _secure_dir(LOCAL)
    path = os.path.join(LOCAL, "link.lock")
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


def load_linkmap(tool):
    with open(os.path.join(REPO_ROOT, "tools", tool, "linkmap.json")) as f:
        return json.load(f)


def link_targets(tool):
    lm = load_linkmap(tool)
    out = []
    for entry in lm.get("links", []):
        dest = _expand_dest(entry["dest"])
        src = os.path.realpath(os.path.join(REPO_ROOT, entry["src"]))
        if not (src == REPO_ROOT or src.startswith(REPO_ROOT + os.sep)):
            raise SystemExit(f"refusing source outside clone: {entry['src']}")
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


def _backup_foreign(dest):
    """Snapshot an explicitly forced regular file under local/, never silently replace it."""
    if not os.path.isfile(dest) or os.path.islink(dest):
        return None
    bdir = os.path.join(LOCAL, "backups", time.strftime("%Y%m%d-%H%M%S"))
    _secure_dir(bdir)
    base = os.path.basename(dest) + ".leos-link"
    target = os.path.join(bdir, base)
    n = 1
    while os.path.lexists(target):
        target = os.path.join(bdir, f"{base}.{n}")
        n += 1
    shutil.copy2(dest, target)
    return target


def _stage_symlink(dest, src):
    """Create the replacement link under local/ and atomically install it.  This avoids deleting
    a predictable ``.tmp-leoslink`` path that could be a user-owned file."""
    stage = os.path.join(LOCAL, "staging")
    _secure_dir(stage)
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    if os.stat(stage).st_dev != os.stat(parent).st_dev:
        raise OSError("local staging and destination are on different filesystems; refusing non-atomic link")
    for _ in range(10):
        tmp = os.path.join(stage, "link-" + secrets.token_hex(16))
        try:
            os.symlink(src, tmp)
            try:
                os.replace(tmp, dest)
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
            return
        except FileExistsError:
            continue
    raise OSError("could not allocate a private local staging path")


def make_link(dest, src, force):
    st = link_state(dest, src)
    if st == "linked":
        return {"dest": dest, "action": "ok"}
    if st == "foreign" and not force:
        return {"dest": dest, "action": "refused",
                "reason": "existing non-symlink content — pass --force after backing it up"}
    if st == "foreign" and os.path.isdir(dest):
        return {"dest": dest, "action": "refused",
                "reason": "existing directory is never replaced automatically; move it after backing it up"}
    backup = _backup_foreign(dest) if st == "foreign" else None
    try:
        _stage_symlink(dest, src)
    except OSError as e:
        return {"dest": dest, "action": "refused", "reason": str(e)}
    return {"dest": dest, "action": "relinked" if st != "missing" else "linked",
            "was": st, "backup": backup}


def main():
    ap = argparse.ArgumentParser(prog="leos-link.py")
    ap.add_argument("--tool", required=True, choices=["claude", "codex", "opencode", "cursor"])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    results = []
    problems = False
    with _link_lock():
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
        if not args.check and not problems:
            _record_configured(args.tool)
    print(json.dumps(results, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
