#!/usr/bin/env python3
"""Tests for core/hooks/inject-instructions.py — the Codex SessionStart instruction injector.

Verifies it emits valid SessionStart additionalContext JSON, self-locates the real
global/AGENTS.md by default, and fails open (exit 0, no output) when the file is missing.
Run: bin/leos-python tests/inject-tests.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TEST_TMP = os.path.join(ROOT, "local", "test-work")
os.makedirs(TEST_TMP, exist_ok=True)
tempfile.tempdir = TEST_TMP
INJ = os.path.join(ROOT, "core", "hooks", "inject-instructions.py")

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def run(env):
    return subprocess.run([sys.executable, INJ], capture_output=True, text=True, env=env)


def main():
    tmp = tempfile.mkdtemp(prefix="injecttest.")

    # 1. valid SessionStart JSON carrying the exact file contents
    gi = os.path.join(tmp, "AGENTS.md")
    with open(gi, "w") as f:
        f.write("# leos rules\nbe good ✅\n")
    r = run(dict(os.environ, LEOS_GLOBAL_INSTRUCTIONS=gi))
    check("exit 0 on success", r.returncode == 0)
    try:
        data = json.loads(r.stdout)
    except Exception:
        data = {}
    hso = data.get("hookSpecificOutput", {})
    check("hookEventName is SessionStart", hso.get("hookEventName") == "SessionStart")
    check("additionalContext == file contents", hso.get("additionalContext") == "# leos rules\nbe good ✅\n")

    # 2. fail-open on a missing file: exit 0, no stdout
    r = run(dict(os.environ, LEOS_GLOBAL_INSTRUCTIONS=os.path.join(tmp, "nope.md")))
    check("missing file exits 0", r.returncode == 0)
    check("missing file emits nothing", r.stdout.strip() == "")

    # 3. default self-location resolves to the real <clone>/global/AGENTS.md
    env = {k: v for k, v in os.environ.items() if k != "LEOS_GLOBAL_INSTRUCTIONS"}
    r = run(env)
    real = os.path.join(ROOT, "global", "AGENTS.md")
    check("self-located injection exits 0", r.returncode == 0)
    try:
        data = json.loads(r.stdout)
        with open(real, encoding="utf-8") as f:
            want = f.read()
        check("self-located additionalContext == real global/AGENTS.md",
              data["hookSpecificOutput"]["additionalContext"] == want)
    except Exception:
        check("self-located additionalContext == real global/AGENTS.md", False)

    total = passed + failed
    print(f"inject-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
