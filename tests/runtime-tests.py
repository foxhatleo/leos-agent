#!/usr/bin/env python3
"""Tests for bin/leos-runtime.py + bin/leos-python — the private-runtime lifecycle.

Fast paths only (no network, no real venv build): explicit-bootstrap hard errors, launcher
LEOS_LOCAL resolution, and the rebuild lock file. Run: bin/leos-python tests/runtime-tests.py
"""

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TEST_TMP = os.path.join(ROOT, "local", "test-work")
os.makedirs(TEST_TMP, exist_ok=True)
tempfile.tempdir = TEST_TMP
RUNTIME = os.path.join(ROOT, "bin", "leos-runtime.py")
LAUNCHER = os.path.join(ROOT, "bin", "leos-python")

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def main():
    local = tempfile.mkdtemp(prefix="rtlocal.")
    env = dict(os.environ, LEOS_LOCAL=local)
    env.pop("LEOS_PYTHON", None)

    # 1. An explicitly requested bootstrap interpreter that does not exist is a hard error naming
    #    the request — never a silent fallback to an ambient python3.
    r = subprocess.run([sys.executable, RUNTIME, "setup", "--python", "/nonexistent/python"],
                       capture_output=True, text=True, env=env)
    check("missing --python is a hard error", r.returncode == 1)
    check("error names the requested interpreter", "/nonexistent/python" in r.stderr)
    check("error rules out ambient fallback", "ambient" in r.stderr)
    check("no venv was staged for the failed request",
          not any(n.startswith(".venv") for n in os.listdir(local)))

    # 2. Same via LEOS_PYTHON.
    r = subprocess.run([sys.executable, RUNTIME, "setup"],
                       capture_output=True, text=True,
                       env=dict(env, LEOS_PYTHON="/nonexistent/lp-python"))
    check("missing LEOS_PYTHON is a hard error", r.returncode == 1 and "/nonexistent/lp-python" in r.stderr)

    # 3. A too-old explicit interpreter is refused the same way (fake a 3.8 via a shim).
    shim = os.path.join(local, "old-python")
    with open(shim, "w") as f:
        f.write('#!/bin/sh\nif [ "$1" = "-c" ]; then echo "[3, 8, 19]"; fi\nexit 0\n')
    os.chmod(shim, os.stat(shim).st_mode | stat.S_IXUSR)
    r = subprocess.run([sys.executable, RUNTIME, "setup", "--python", shim],
                       capture_output=True, text=True, env=env)
    check("too-old explicit interpreter is a hard error", r.returncode == 1 and "old-python" in r.stderr)

    # 4. bin/leos-python honors LEOS_LOCAL: a shim venv python there is what gets exec'd...
    shim_venv = os.path.join(local, ".venv", "bin")
    os.makedirs(shim_venv)
    shim_py = os.path.join(shim_venv, "python")
    with open(shim_py, "w") as f:
        f.write('#!/bin/sh\necho "shim-python ran"\nexit 0\n')
    os.chmod(shim_py, os.stat(shim_py).st_mode | stat.S_IXUSR)
    r = subprocess.run([LAUNCHER, "-V"], capture_output=True, text=True, env=env)
    check("launcher execs the LEOS_LOCAL venv python", r.returncode == 0 and "shim-python ran" in r.stdout)

    # 5. ...and exits 127 with a rebuild hint when that venv is absent.
    empty_local = tempfile.mkdtemp(prefix="rtempty.")
    r = subprocess.run([LAUNCHER, "-V"], capture_output=True, text=True,
                       env=dict(os.environ, LEOS_LOCAL=empty_local))
    check("launcher fails closed without the LEOS_LOCAL venv",
          r.returncode == 127 and "leos-runtime.py setup" in r.stderr)

    # 6. The rebuild lock is real: entering _runtime_lock creates local/runtime.lock mode 0600.
    spec = importlib.util.spec_from_file_location("leos_runtime_t", RUNTIME)
    module = importlib.util.module_from_spec(spec)
    lock_local = tempfile.mkdtemp(prefix="rtlock.")
    os.environ["LEOS_LOCAL"] = lock_local
    try:
        spec.loader.exec_module(module)
        with module._runtime_lock():
            lock_path = os.path.join(lock_local, "runtime.lock")
            check("runtime lock file exists while held", os.path.isfile(lock_path))
            check("runtime lock file is private",
                  stat.S_IMODE(os.stat(lock_path).st_mode) == 0o600)
    finally:
        os.environ.pop("LEOS_LOCAL", None)

    total = passed + failed
    print(f"runtime-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    import shutil
    for d in (local, empty_local, lock_local):
        shutil.rmtree(d, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
