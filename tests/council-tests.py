#!/usr/bin/env python3
"""Tests for core/council/bin/council.py — risk scoring, the delta-aware Stop-hook nudge, the
recursion sentinel, the in-review pointer, the persistent loop guard, and the seat-flag doctor
check.

Each group uses its OWN throwaway git repo + isolated STATE (LEOS_COUNCIL_STATE) so nothing
touches real state and groups don't contaminate each other's markers/baselines. Temp repos set
commit.gpgsign=false so they pass on machines with global signing on.
Run: python3 tests/council-tests.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
BIN = os.path.join(ROOT, "core", "council", "bin", "council.py")

passed = failed = 0
_cleanup = []


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def make_repo():
    """Fresh git repo (one 'base' commit) + isolated council state. Returns (repo, env)."""
    repo = tempfile.mkdtemp(prefix="councilrepo.")
    state = tempfile.mkdtemp(prefix="councilstate.")
    _cleanup.extend([repo, state])
    env = dict(os.environ, LEOS_COUNCIL_STATE=state,
               LEOS_COUNCIL_CONFIG=os.path.join(state, "config.json"),
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    env.pop("LEOS_COUNCIL_SEAT", None)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "commit.gpgsign", "false")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("# base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo, env


def council(repo, env, *args, stdin=None):
    r = subprocess.run(["python3", BIN, *args], cwd=repo, capture_output=True, text=True,
                       env=env, input=stdin)
    return r.returncode, r.stdout, r.stderr


def write_code(repo, name, n, prefix="x"):
    with open(os.path.join(repo, name), "w") as f:
        f.write("def f():\n" + "\n".join(f"    {prefix}{i} = {i}" for i in range(n)) + "\n")


def append(repo, name, n, prefix="g"):
    with open(os.path.join(repo, name), "a") as f:
        f.write("\n# more\n" + "\n".join(f"{prefix}{i} = 1" for i in range(n)) + "\n")


def risk(repo, env):
    _, out, _ = council(repo, env, "risk", "--json")
    return json.loads(out)


def hook(repo, env):
    ec, _, err = council(repo, env, "hook", stdin=json.dumps({"cwd": repo}))
    return ec, err


def main():
    # 1. docs-only change -> skip
    repo, env = make_repo()
    with open(os.path.join(repo, "README.md"), "a") as f:
        f.write("more docs\n")
    check("docs-only is skip", risk(repo, env)["tier"] == "skip")

    # 2. a real (non-trivial) code change -> elevated+  (raised is_small bar = 120 lines)
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    check("code change is elevated+", risk(repo, env)["tier_index"] >= 2)

    # 3. sentinel suppresses the hook even on an elevated diff
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    senv = dict(env, LEOS_COUNCIL_SEAT="1")
    ec, _, _ = council(repo, senv, "hook", stdin=json.dumps({"cwd": repo}))
    check("sentinel suppresses hook (exit 0)", ec == 0)

    # 4. without a marker -> nudge (exit 42) + seat-warning text
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    ec, err = hook(repo, env)
    check("nudge fires with no marker", ec == 42)
    check("nudge text warns seats to ignore", "council seat" in err.lower())

    # 5. begin (in-review pointer) suppresses the nudge
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    council(repo, env, "begin", "--checkpoint", "impl")
    ec, _ = hook(repo, env)
    check("in-review pointer suppresses nudge", ec == 0)

    # 6. an impl mark clears the nudge (no follow-up edit -> snapshot == reviewed tree)
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    council(repo, env, "mark", "--checkpoint", "impl", "--tier", "elevated")
    ec, _ = hook(repo, env)
    check("impl mark clears nudge", ec == 0)

    # 7. DELTA-AWARENESS (headline): after a review, a tiny follow-up does NOT re-trigger,
    #    but a substantial incremental change DOES.
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    council(repo, env, "mark", "--checkpoint", "impl", "--tier", "elevated")
    with open(os.path.join(repo, "app.py"), "a") as f:
        f.write("\n# tiny fix\nz = 1\n")
    ec, _ = hook(repo, env)
    check("tiny follow-up after review does NOT re-nudge", ec == 0)
    append(repo, "app.py", 200, prefix="h")   # big incremental change
    ec, _ = hook(repo, env)
    check("big incremental change after review DOES nudge", ec == 42)

    # 8. NO-REMOTE / non-standard branch -> no ambiguous escalation
    repo, env = make_repo()
    git(repo, "branch", "-m", "solo")          # no main/master/develop/trunk, no upstream
    write_code(repo, "app.py", 60)             # small (< 120 lines) code change, no test file
    r = risk(repo, env)
    check("no-remote small code change is low (not escalated)", r["tier_index"] == 1)
    with open(os.path.join(repo, "app.py"), "w") as f:
        f.truncate()
    os.remove(os.path.join(repo, "app.py"))
    with open(os.path.join(repo, "NOTES.md"), "w") as f:
        f.write("# just docs\nlots of notes\n")
    check("no-remote docs-only is skip (not escalated)", risk(repo, env)["tier"] == "skip")
    ec, _ = hook(repo, env)
    check("no-remote docs-only hook is silent", ec == 0)

    # 9. LOOP-GUARD PERSISTENCE across diff-hash churn (no baseline)
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    n42 = 0
    for i in range(5):
        ec, _ = hook(repo, env)
        if ec == 42:
            n42 += 1
        with open(os.path.join(repo, "app.py"), "a") as f:  # churn the diff hash by 1 line
            f.write(f"c{i} = 1\n")
    check("persistent loop guard caps nudges across hash churn", n42 == 2)

    # 9b. RE-ARM: after the guard caps at MAX_NUDGES, a genuinely large NEW increment (since the
    #     guard's anchor tree) re-arms it and nudges again — a small increment must not.
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    capped = [hook(repo, env)[0] for _ in range(3)]     # 42, 42, then capped 0
    check("guard caps at MAX_NUDGES before re-arm", capped == [42, 42, 0])
    append(repo, "app.py", 200, prefix="rearm")         # big new work past the anchor
    ec, _ = hook(repo, env)
    check("large new work after cap re-arms the guard (nudges again)", ec == 42)

    # 10. CACHE correctness: two consecutive risk calls agree + cache file exists
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    r1 = risk(repo, env)
    r2 = risk(repo, env)
    check("cached risk is stable", r1["tier"] == r2["tier"] and r1["hash"] == r2["hash"])
    _, sd, _ = council(repo, env, "state-dir")
    check("risk cache file written", os.path.exists(os.path.join(sd.strip(), "cache.json")))

    # 11. reviewed-tree delta suppression when the reviewed work is COMMITTED and still fills the
    #     full diff (feature branch: merge-base excludes the work). full risk stays elevated (no
    #     early return), so ONLY the baseline-delta path can suppress the tiny follow-up — and the
    #     missing-baseline control proves it (the same elevated diff nudges once the baseline is gone).
    repo, env = make_repo()
    git(repo, "checkout", "-q", "-b", "feature")
    write_code(repo, "app.py", 200)
    council(repo, env, "mark", "--checkpoint", "impl", "--tier", "elevated")   # baseline = 200-line tree
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "reviewed work")   # HEAD moves; work committed but still in full diff
    with open(os.path.join(repo, "app.py"), "a") as f:
        f.write("\n# tiny post-commit fix\nq = 1\n")
    check("committed reviewed work still scores elevated in the full diff", risk(repo, env)["tier_index"] >= 2)
    ec, _ = hook(repo, env)
    check("baseline delta suppresses tiny edit on committed reviewed work", ec == 0)
    _, sd, _ = council(repo, env, "state-dir")
    os.remove(os.path.join(sd.strip(), "baseline-impl.json"))   # control: drop the reviewed baseline
    ec, _ = hook(repo, env)
    check("without the reviewed baseline the same elevated diff DOES nudge", ec == 42)

    # 11b. a reviewed baseline whose tree object no longer resolves (gc-pruned) must NOT be trusted
    #      as a trivial delta — it falls through to a full-risk nudge instead of silent suppression.
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    council(repo, env, "mark", "--checkpoint", "impl", "--tier", "elevated")
    _, sd, _ = council(repo, env, "state-dir")
    bp = os.path.join(sd.strip(), "baseline-impl.json")
    b = json.load(open(bp)); b["reviewed_tree"] = "0" * 40; json.dump(b, open(bp, "w"))  # unresolvable
    with open(os.path.join(repo, "app.py"), "a") as f:
        f.write("\nextra = 1\n")   # ensure cur != reviewed_tree
    ec, _ = hook(repo, env)
    check("unresolvable reviewed_tree falls through to a nudge (not silently suppressed)", ec == 42)

    # 12. unborn HEAD (fresh init, no commits) doesn't crash or over-escalate
    ur = tempfile.mkdtemp(prefix="councilunborn.")
    us = tempfile.mkdtemp(prefix="councilunbornstate.")
    _cleanup.extend([ur, us])
    uenv = dict(os.environ, LEOS_COUNCIL_STATE=us,
                LEOS_COUNCIL_CONFIG=os.path.join(us, "config.json"),
                GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    uenv.pop("LEOS_COUNCIL_SEAT", None)
    git(ur, "init", "-q")
    git(ur, "config", "user.email", "t@t.t")
    git(ur, "config", "user.name", "t")
    with open(os.path.join(ur, "NOTES.md"), "w") as f:
        f.write("# docs only\n")
    git(ur, "add", "-A")
    ec, out, _ = council(ur, uenv, "risk", "--json")
    check("unborn HEAD risk does not crash", ec == 0 and json.loads(out)["tier"] == "skip")
    ec, _ = hook(ur, uenv)
    check("unborn HEAD hook is silent for docs", ec == 0)

    # 13. seat-flag doctor check: a claude seat WITHOUT --safe-mode is flagged
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

    # 14. doctor: instruction-delivery + retired-symlink + malformed-config checks (new logic).
    dhome = tempfile.mkdtemp(prefix="doctorhome.")
    _cleanup.append(dhome)
    gi = os.path.join(ROOT, "global", "AGENTS.md")
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = dhome
    try:
        os.makedirs(os.path.join(dhome, ".claude"))
        with open(os.path.join(dhome, ".claude", "CLAUDE.md"), "w") as f:
            f.write("# mine\n")   # user content, no leos block
        probs, tinfo = [], {}
        doc.check_instructions_delivery("claude", probs, tinfo)
        check("doctor flags missing claude @import block",
              tinfo.get("instructions") == "missing" and any("leos-block" in p for p in probs))
        with open(os.path.join(dhome, ".claude", "CLAUDE.md"), "w") as f:
            f.write(f"# mine\n\n<!-- leos-agent:global-instructions BEGIN -->\n@{gi}\n"
                    f"<!-- leos-agent:global-instructions END -->\n")
        probs, tinfo = [], {}
        doc.check_instructions_delivery("claude", probs, tinfo)
        check("doctor passes present claude @import block",
              tinfo.get("instructions") == "ok" and not probs)
        # retired-symlink hygiene: a leftover clone-symlink at ~/.codex/AGENTS.md is flagged
        os.makedirs(os.path.join(dhome, ".codex"))
        os.symlink(gi, os.path.join(dhome, ".codex", "AGENTS.md"))
        probs, rep = [], []
        doc.check_tool("codex", probs, rep)
        check("doctor flags leftover retired clone-symlink",
              any("leftover clone-symlink" in p for p in probs))
        # malformed opencode.json must be reported, not crash doctor
        os.makedirs(os.path.join(dhome, ".config", "opencode"))
        with open(os.path.join(dhome, ".config", "opencode", "opencode.json"), "w") as f:
            f.write("{ not valid json ")
        probs, tinfo = [], {}
        doc.check_instructions_delivery("opencode", probs, tinfo)
        check("doctor tolerates a malformed opencode.json (reports, no crash)",
              tinfo.get("instructions") == "missing")
    finally:
        if saved_home is not None:
            os.environ["HOME"] = saved_home

    total = passed + failed
    print(f"council-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    for d in _cleanup:
        shutil.rmtree(d, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
