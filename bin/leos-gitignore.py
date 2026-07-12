#!/usr/bin/env python3
"""Ensure council control files are ignored by Git globally.

Uses the configured ``core.excludesFile`` when present. If it is unset, configures Git to use
``~/.gitignore``. Existing files are backed up under gitignored ``local/backups`` before an atomic,
additive update. The operation is idempotent and refuses destinations outside HOME.
"""

import contextlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LOCAL = os.environ.get("LEOS_LOCAL", os.path.join(REPO_ROOT, "local"))
HOME = os.path.realpath(os.path.expanduser("~"))
ENTRIES = (".council-off", ".council.json")


def _git(*args):
    return subprocess.run(["git", "config", "--global", *args], capture_output=True, text=True)


def _under_home(path):
    return path == HOME or path.startswith(HOME + os.sep)


def _resolve(value):
    path = os.path.realpath(os.path.expanduser(value))
    if not _under_home(path):
        raise SystemExit(f"refusing global excludes file outside HOME: {value}")
    if os.path.isdir(path):
        raise SystemExit(f"refusing directory as global excludes file: {value}")
    return path


def _backup(path, label):
    if not os.path.exists(path):
        return None
    bdir = os.path.join(LOCAL, "backups", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bdir, exist_ok=True, mode=0o700)
    base = os.path.basename(path) + "." + label
    target = os.path.join(bdir, base)
    n = 1
    while os.path.lexists(target):
        target = os.path.join(bdir, f"{base}.{n}")
        n += 1
    shutil.copy2(path, target)
    return target


def _write(path, text):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    staging = os.path.join(LOCAL, "staging")
    os.makedirs(staging, exist_ok=True, mode=0o700)
    if os.stat(staging).st_dev != os.stat(parent).st_dev:
        raise SystemExit("local staging and global excludes file are on different filesystems")
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o600
    fd, tmp = tempfile.mkstemp(prefix="gitignore-", dir=staging)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _lock():
    os.makedirs(LOCAL, exist_ok=True, mode=0o700)
    path = os.path.join(LOCAL, "gitignore.lock")
    with open(path, "a+") as lock:
        try:
            os.chmod(path, 0o600)
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


def _global_config_path():
    override = os.environ.get("GIT_CONFIG_GLOBAL")
    return os.path.realpath(os.path.expanduser(override or "~/.gitconfig"))


def main():
    with _lock():
        found = _git("--path", "--get", "core.excludesFile")
        if found.returncode not in (0, 1):
            print(found.stderr.strip() or "cannot read global core.excludesFile", file=sys.stderr)
            return 1

        configured = found.returncode == 0 and bool(found.stdout.strip())
        value = found.stdout.strip() if configured else "~/.gitignore"
        path = _resolve(value)

        try:
            current = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        except OSError as exc:
            print(f"cannot read global excludes file {path}: {exc}", file=sys.stderr)
            return 1

        present = {line.strip() for line in current.splitlines()}
        missing = [entry for entry in ENTRIES if entry not in present]

        if not configured:
            config_path = _global_config_path()
            if not _under_home(config_path):
                print(f"refusing global Git config outside HOME: {config_path}", file=sys.stderr)
                return 1
            _backup(config_path, "git-config")
            set_result = _git("core.excludesFile", "~/.gitignore")
            if set_result.returncode != 0:
                print(set_result.stderr.strip() or "cannot configure core.excludesFile", file=sys.stderr)
                return 1

        if missing:
            _backup(path, "global-excludes")
            prefix = current
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
            _write(path, prefix + "".join(entry + "\n" for entry in missing))
            print(f"updated global excludes file: {path}")
        else:
            print(f"global excludes already configured: {path}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
