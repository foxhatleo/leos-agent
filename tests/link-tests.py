#!/usr/bin/env python3
"""Tests for bin/leos-link.py — the symlink farm.

Covers: fresh link, idempotence, refuse-to-clobber a foreign regular file, --force replace,
wrong-link repair, dangling detection, and that link_state never follows the link (lstat/readlink).
Run: bin/leos-python tests/link-tests.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TEST_TMP = os.path.join(ROOT, "local", "test-work")
os.makedirs(TEST_TMP, exist_ok=True)
tempfile.tempdir = TEST_TMP
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
    args = [sys.executable, LINK, "--tool", "claude"]
    if force:
        args.append("--force")
    if check_only:
        args.append("--check")
    r = subprocess.run(args, capture_output=True, text=True,
                       env=dict(os.environ, HOME=home, LEOS_LOCAL=os.path.join(home, "local")))
    return r.returncode


def main():
    home = tempfile.mkdtemp(prefix="linkhome.")

    # 1. fresh link
    rc = run_cli(home)
    check("fresh link exits 0", rc == 0)
    dest = os.path.join(home, ".claude", "hooks", "bash-guard.py")
    check("symlink created", os.path.islink(dest))
    check("symlink points into clone", os.path.realpath(dest) == os.path.join(ROOT, "core", "hooks", "bash-guard.py"))
    private_python = os.path.join(home, ".claude", "leos-python")
    launched = subprocess.run([private_python, "-c", "import sys; print(sys.executable)"], capture_output=True, text=True)
    check("symlinked launcher resolves clone-private venv", launched.returncode == 0 and
          os.path.realpath(launched.stdout.strip()) == os.path.realpath(os.path.join(ROOT, "local", ".venv", "bin", "python")))

    # 2. idempotent
    rc = run_cli(home)
    check("re-link idempotent (exit 0)", rc == 0)
    rc = run_cli(home, check_only=True)
    check("--check passes", rc == 0)

    # A formerly predictable sibling temp path is user-owned data, never a cleanup target.
    old_tmp = dest + ".tmp-leoslink"
    with open(old_tmp, "w") as f:
        f.write("leave me alone\n")
    rc = run_cli(home)
    check("preexisting legacy tmp path is preserved", rc == 0 and open(old_tmp).read() == "leave me alone\n")

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

    # 7. CODEX_HOME is honored consistently instead of hard-coding ~/.codex.
    codex_home = os.path.join(home, "relocated-codex")
    env = dict(os.environ, HOME=home, CODEX_HOME=codex_home, LEOS_LOCAL=os.path.join(home, "local"))
    r = subprocess.run([sys.executable, LINK, "--tool", "codex"], capture_output=True, text=True, env=env)
    cguard = os.path.join(codex_home, "hooks", "bash-guard.py")
    check("CODEX_HOME link install succeeds", r.returncode == 0)
    check("CODEX_HOME receives Codex hooks", os.path.islink(cguard) and
          os.path.realpath(cguard) == os.path.join(ROOT, "core", "hooks", "bash-guard.py"))

    # A shared council skill link appears in multiple host link maps, but must not make doctor
    # classify every consumer host as configured. At this point only Claude and Codex were linked.
    doctor = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "leos-doctor.py")],
                            capture_output=True, text=True, env=env)
    report = json.loads(doctor.stdout).get("report", [])
    configured = {item.get("tool") for item in report if item.get("configured")}
    check("shared skill link does not imply other configured hosts",
          configured == {"claude", "codex"})

    # 8. OpenCode's automatic plugin discovery is the plural plugins/ directory.
    env = dict(os.environ, HOME=home, LEOS_LOCAL=os.path.join(home, "local"))
    r = subprocess.run([sys.executable, LINK, "--tool", "opencode"], capture_output=True, text=True, env=env)
    plugin = os.path.join(home, ".config", "opencode", "plugins", "leos-guard.ts")
    check("OpenCode plugin map installs successfully", r.returncode == 0)
    check("OpenCode plugin uses plural plugins directory", os.path.islink(plugin) and
          os.path.realpath(plugin) == os.path.join(ROOT, "tools", "opencode", "plugin", "leos-guard.ts"))

    # 9. Cursor gets the same private launcher the adapter expects, not a bare system Python.
    r = subprocess.run([sys.executable, LINK, "--tool", "cursor"], capture_output=True, text=True, env=env)
    cursor_launcher = os.path.join(home, ".cursor", "leos-python")
    check("Cursor link install succeeds", r.returncode == 0)
    check("Cursor private launcher linked", os.path.islink(cursor_launcher) and
          os.path.realpath(cursor_launcher) == os.path.join(ROOT, "bin", "leos-python"))

    total = passed + failed
    print(f"link-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    import shutil
    shutil.rmtree(home, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
