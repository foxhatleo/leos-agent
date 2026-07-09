#!/usr/bin/env python3
"""Tests for bin/leos-link.py — the symlink farm.

Covers: fresh link, idempotence, refuse-to-clobber a foreign regular file, --force replace,
wrong-link repair, dangling detection, and that link_state never follows the link (lstat/readlink).
Run: python3 tests/link-tests.py
"""

import importlib.util
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LINK = os.path.join(ROOT, "bin", "leos-link.py")

spec = importlib.util.spec_from_file_location("leos_link", LINK)
ll = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ll)

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def run_cli(home, force=False, check_only=False):
    args = ["python3", LINK, "--tool", "claude"]
    if force:
        args.append("--force")
    if check_only:
        args.append("--check")
    r = subprocess.run(args, capture_output=True, text=True, env=dict(os.environ, HOME=home))
    return r.returncode


def main():
    home = tempfile.mkdtemp(prefix="linkhome.")

    # 1. fresh link
    rc = run_cli(home)
    check("fresh link exits 0", rc == 0)
    dest = os.path.join(home, ".claude", "hooks", "bash-guard.py")
    check("symlink created", os.path.islink(dest))
    check("symlink points into clone", os.path.realpath(dest) == os.path.join(ROOT, "core", "hooks", "bash-guard.py"))

    # 2. idempotent
    rc = run_cli(home)
    check("re-link idempotent (exit 0)", rc == 0)
    rc = run_cli(home, check_only=True)
    check("--check passes", rc == 0)

    # 3. refuse to clobber a foreign regular file
    os.remove(dest)
    with open(dest, "w") as f:
        f.write("# user's own file\n")
    rc = run_cli(home)
    check("refuses foreign file (exit 1)", rc == 1)
    check("foreign file untouched", not os.path.islink(dest) and open(dest).read().startswith("# user"))

    # 4. --force replaces it
    rc = run_cli(home, force=True)
    check("--force replaces foreign file", rc == 0 and os.path.islink(dest))

    # 5. wrong-link repair
    os.remove(dest)
    os.symlink("/nowhere/else.py", dest)
    st = ll.link_state(dest, os.path.join(ROOT, "core", "hooks", "bash-guard.py"))
    check("wrong-link detected", st == "wrong-link")
    rc = run_cli(home)   # relinks non-foreign symlinks without --force
    check("wrong-link repaired", os.path.realpath(dest) == os.path.join(ROOT, "core", "hooks", "bash-guard.py"))

    # 6. dangling detection (link to a missing src) without following the link
    dpath = os.path.join(home, "dangle")
    os.symlink(os.path.join(home, "missing-src"), dpath)
    st = ll.link_state(dpath, os.path.join(home, "missing-src"))
    check("dangling detected (not followed)", st == "dangling")

    total = passed + failed
    print(f"link-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    import shutil
    shutil.rmtree(home, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
