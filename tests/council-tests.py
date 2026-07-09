#!/usr/bin/env python3
"""Tests for core/council/bin/council.py — risk scoring, the Stop-hook nudge, the
recursion sentinel, the in-review marker, and the seat-flag doctor check.

Uses a throwaway git repo + isolated STATE (LEOS_COUNCIL_STATE) so nothing touches real state.
The temp-repo fixture sets commit.gpgsign=false so it passes on machines with global signing on.
Run: python3 tests/council-tests.py
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
BIN = os.path.join(ROOT, "core", "council", "bin", "council.py")

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def council(repo, env, *args, stdin=None):
    r = subprocess.run(["python3", BIN, *args], cwd=repo, capture_output=True, text=True,
                       env=env, input=stdin)
    return r.returncode, r.stdout, r.stderr


def main():
    repo = tempfile.mkdtemp(prefix="councilrepo.")
    state = tempfile.mkdtemp(prefix="councilstate.")
    env = dict(os.environ, LEOS_COUNCIL_STATE=state,
               LEOS_COUNCIL_CONFIG=os.path.join(state, "config.json"),
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "commit.gpgsign", "false")   # pass with global signing on
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("# base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")

    # 1. docs-only change -> skip
    with open(os.path.join(repo, "README.md"), "a") as f:
        f.write("more docs\n")
    ec, out, _ = council(repo, env, "risk", "--json")
    risk = json.loads(out)
    check("docs-only is skip", risk["tier"] == "skip")

    # 2. a real code change -> elevated+ (non-trivial, no tests)
    with open(os.path.join(repo, "app.py"), "w") as f:
        f.write("def f():\n" + "\n".join(f"    x{i} = {i}" for i in range(80)) + "\n")
    ec, out, _ = council(repo, env, "risk", "--json")
    risk = json.loads(out)
    check("code change is elevated+", risk["tier_index"] >= 2)

    hook_payload = json.dumps({"cwd": repo})

    # 3. sentinel suppresses the hook even on an elevated diff
    senv = dict(env, LEOS_COUNCIL_SEAT="1")
    ec, _, _ = council(repo, senv, "hook", stdin=hook_payload)
    check("sentinel suppresses hook (exit 0)", ec == 0)

    # 4. without a marker -> nudge (exit 42)
    ec, _, err = council(repo, env, "hook", stdin=hook_payload)
    check("nudge fires with no marker", ec == 42)
    check("nudge text warns seats to ignore", "council seat" in err.lower())

    # 5. begin (in-review marker) suppresses the nudge
    council(repo, env, "begin", "--checkpoint", "impl")
    ec, _, _ = council(repo, env, "hook", stdin=hook_payload)
    check("in-review marker suppresses nudge", ec == 0)

    # 6. an impl mark clears the nudge permanently
    council(repo, env, "mark", "--checkpoint", "impl", "--tier", "elevated")
    ec, _, _ = council(repo, env, "hook", stdin=hook_payload)
    check("impl mark clears nudge", ec == 0)

    # 7. loop guard: fresh diff, nudge at most twice
    with open(os.path.join(repo, "app.py"), "a") as f:
        f.write("\n# change\n" + "\n".join(f"g{i}=1" for i in range(80)) + "\n")
    n42 = 0
    for _ in range(4):
        ec, _, _ = council(repo, env, "hook", stdin=hook_payload)
        if ec == 42:
            n42 += 1
    check("loop guard caps nudges at 2", n42 == 2)

    # 8. seat-flag doctor check: a claude seat WITHOUT --safe-mode is flagged
    sys.path.insert(0, os.path.join(ROOT, "bin"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("leos_doctor", os.path.join(ROOT, "bin", "leos-doctor.py"))
    doc = importlib.util.module_from_spec(spec); spec.loader.exec_module(doc)
    bad = doc.check_seat_flags("claude", {"seats": [
        {"name": "opus", "argv": ["claude", "--print", "--model", "opus"]}]})
    check("doctor flags claude seat missing --safe-mode", any("safe-mode" in p for p in bad))
    good = doc.check_seat_flags("claude", {"seats": [
        {"name": "opus", "argv": ["claude", "--safe-mode", "--print", "--model", "opus"]}]})
    check("doctor passes claude seat with --safe-mode", not good)

    total = passed + failed
    print(f"council-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    import shutil
    shutil.rmtree(repo, ignore_errors=True)
    shutil.rmtree(state, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
