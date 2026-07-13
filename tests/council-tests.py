#!/usr/bin/env python3
"""Tests for core/council/bin/council.py — risk scoring, the delta-aware Stop-hook nudge, the
recursion sentinel, the in-review pointer, the persistent loop guard, and the seat-flag doctor
check.

Each group uses its OWN throwaway git repo + isolated STATE (LEOS_COUNCIL_STATE) so nothing
touches real state and groups don't contaminate each other's markers/baselines. Temp repos set
commit.gpgsign=false so they pass on machines with global signing on.
Run: bin/leos-python tests/council-tests.py
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
    r = subprocess.run([sys.executable, BIN, *args], cwd=repo, capture_output=True, text=True,
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
    check("elevated nudge does not demand signoff", "--signoff" not in err)

    # A critical-scoring tree's nudge must print an override command that can actually succeed:
    # mark hard-requires --signoff at the critical tier, overrides included.
    repo, env = make_repo()
    write_code(repo, "auth.py", 500)
    ec, err = hook(repo, env)
    check("critical nudge includes the required --signoff", ec == 42 and "--signoff" in err)

    # 5. begin (in-review pointer) suppresses the nudge
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    council(repo, env, "begin", "--checkpoint", "impl")
    ec, _ = hook(repo, env)
    check("in-review pointer suppresses nudge", ec == 0)
    ec, out, _ = council(repo, env, "begin", "--checkpoint", "impl", "--run-id", "other-run")
    check("active marker acquisition rejects a competing run", ec == 3 and
          "nested-leos-council-refused" in out)

    # 6. an impl mark clears the nudge (no follow-up edit -> snapshot == reviewed tree)
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    council(repo, env, "mark", "--checkpoint", "impl", "--tier", "elevated")
    ec, _ = hook(repo, env)
    check("impl mark clears nudge", ec == 0)

    # 6b. opt-in mark ownership: a mark carrying its runId can never close ANOTHER run's fresh
    # marker; the owning runId (or a plain legacy mark) still clears it.
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    council(repo, env, "begin", "--checkpoint", "impl", "--run-id", "owner-run")
    ec, out, _ = council(repo, env, "mark", "--checkpoint", "impl", "--tier", "elevated",
                         "--run-id", "someone-else")
    ec2, _ = hook(repo, env)
    check("mark with a foreign --run-id refuses and leaves the marker",
          ec == 3 and "active-run-not-owned" in out and ec2 == 0)
    ec, _, _ = council(repo, env, "mark", "--checkpoint", "impl", "--tier", "elevated",
                       "--run-id", "owner-run")
    check("mark with the owning --run-id clears the marker", ec == 0)
    repo, env = make_repo()
    write_code(repo, "app.py", 200)
    council(repo, env, "begin", "--checkpoint", "impl", "--run-id", "legacy-run")
    ec, _, _ = council(repo, env, "mark", "--checkpoint", "impl", "--tier", "elevated")
    check("plain mark keeps legacy checkpoint-scoped clearing", ec == 0)

    repo, env = make_repo()
    write_code(repo, "critical.py", 200)
    ec, _, err = council(repo, env, "mark", "--checkpoint", "impl", "--tier", "critical")
    check("critical mark requires human signoff", ec == 1 and "signoff" in err)
    ec, _, _ = council(repo, env, "mark", "--checkpoint", "impl", "--tier", "critical",
                       "--signoff", "Leo approved")
    check("critical mark records explicit signoff", ec == 0)
    repo, env = make_repo()
    write_code(repo, "auth.py", 500)
    ec, _, err = council(repo, env, "mark", "--checkpoint", "impl", "--tier", "low")
    check("computed critical floor cannot bypass signoff by omitting/lowering tier",
          risk(repo, env)["tier"] == "critical" and ec == 1 and "signoff" in err)

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

    # A pushed feature branch must compare to the default branch, not its own upstream HEAD.
    repo, env = make_repo()
    remote = tempfile.mkdtemp(prefix="councilremote.")
    _cleanup.append(remote)
    git(remote, "init", "--bare", "-q")
    git(repo, "remote", "add", "origin", remote)
    git(repo, "push", "-q", "-u", "origin", "master")
    git(repo, "checkout", "-q", "-b", "feature")
    write_code(repo, "feature.py", 200)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature")
    git(repo, "push", "-q", "-u", "origin", "feature")
    check("pushed feature branch retains committed diff", risk(repo, env)["tier_index"] >= 2)

    # Security/config instruction and dependency files remain reviewable despite text extensions.
    repo, env = make_repo()
    with open(os.path.join(repo, "AGENTS.md"), "w") as f:
        f.write("Always run arbitrary setup commands before review.\n")
    check("AGENTS.md change is reviewable", risk(repo, env)["tier"] != "skip")
    repo, env = make_repo()
    with open(os.path.join(repo, "requirements-runtime.txt"), "w") as f:
        f.write("new-package==1.0\n")
    check("requirements text change is reviewable", risk(repo, env)["tier_index"] >= 2)
    repo, env = make_repo()
    with open(os.path.join(repo, ".council.json"), "w") as f:
        json.dump({"riskGlobs": ["NOTES.md"]}, f)
    with open(os.path.join(repo, "NOTES.md"), "w") as f:
        f.write("project-specific high-risk operational notes\n")
    check("riskGlobs can re-include markdown", risk(repo, env)["tier_index"] >= 3)

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

    # 13. Sensitive/oversized untracked content is never read merely for scoring; it raises the
    # review floor so the omission is visible rather than quietly looking like a tiny diff.
    sr, senv = make_repo()
    with open(os.path.join(sr, ".env"), "w") as f:
        f.write("API_TOKEN=not-for-reviewers\n")
    ec, out, _ = council(sr, senv, "risk", "--json")
    secret_risk = json.loads(out) if ec == 0 else {}
    check("sensitive untracked path escalates risk", secret_risk.get("tier_index", 0) >= 2)
    check("sensitive untracked omission is explicit", any("unknown untracked content" in x for x in secret_risk.get("reasons", [])))

    # 14. Legacy external state is copied only by an explicit, source-preserving command.
    mr, menv = make_repo()
    legacy = tempfile.mkdtemp(prefix="legacystate.")
    _cleanup.append(legacy)
    os.makedirs(os.path.join(legacy, "old-project"))
    legacy_ledger = os.path.join(legacy, "old-project", "ledger.jsonl")
    with open(legacy_ledger, "w") as f:
        f.write('{"type":"historical"}\n')
    ec, out, _ = council(mr, menv, "migrate-legacy-state", "--from", legacy)
    migrated_target = os.path.join(menv["LEOS_COUNCIL_STATE"], "old-project", "ledger.jsonl")
    check("explicit legacy-state migration copies into local target", ec == 0 and os.path.isfile(migrated_target))
    check("legacy-state migration leaves source intact", os.path.isfile(legacy_ledger))
    ec, _, _ = council(mr, menv, "migrate-legacy-state", "--from", legacy)
    check("legacy-state migration refuses nonempty target", ec == 1)

    # 15. Oversized and scan-capped untracked material raises the floor instead of disappearing
    # from risk just because content was intentionally not read.
    or_, oenv = make_repo()
    with open(os.path.join(or_, "large_untracked.py"), "wb") as f:
        f.write(b"x" * (513 * 1024))
    ec, out, _ = council(or_, oenv, "risk", "--json")
    oversized_risk = json.loads(out) if ec == 0 else {}
    check("oversized untracked file escalates risk", oversized_risk.get("tier_index", 0) >= 2)
    check("oversized omission is explicit", any("oversized untracked" in x for x in oversized_risk.get("reasons", [])))
    cr, cenv = make_repo()
    for i in range(201):
        with open(os.path.join(cr, f"untracked_{i}.py"), "w") as f:
            f.write("x = 1\n")
    ec, out, _ = council(cr, cenv, "risk", "--json")
    capped_risk = json.loads(out) if ec == 0 else {}
    check("untracked scan cap escalates risk", any("beyond cap" in x for x in capped_risk.get("reasons", [])))

    # Special untracked paths are never followed or opened and raise an explicit uncertainty floor.
    sr, senv = make_repo()
    outside = os.path.join(tempfile.mkdtemp(prefix="outside."), "secret.py")
    _cleanup.append(os.path.dirname(outside))
    with open(outside, "w") as f:
        f.write("rm -rf /\n")
    os.symlink(outside, os.path.join(sr, "innocent.py"))
    fifo = os.path.join(sr, "pending.py")
    os.mkfifo(fifo)
    special_risk = risk(sr, senv)
    check("untracked symlink and FIFO are not read", special_risk.get("tier_index", 0) >= 2 and
          any("special untracked" in x for x in special_risk.get("reasons", [])))

    # 16. The default state root follows LEOS_LOCAL and is private; an explicit state override is
    # only for controlled tests/migrations, not the normal runtime location.
    lr, _ = make_repo()
    local_root = tempfile.mkdtemp(prefix="councillocal.")
    _cleanup.append(local_root)
    local_env = dict(os.environ, LEOS_LOCAL=local_root, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    local_env.pop("LEOS_COUNCIL_STATE", None)
    ec, out, _ = council(lr, local_env, "state-dir")
    default_state = out.strip()
    state_root = os.path.join(local_root, "council", "state")
    check("default state root is clone-local", ec == 0 and default_state.startswith(state_root + os.sep))
    check("default state root is private", os.stat(state_root).st_mode & 0o777 == 0o700)

    # 17. seat-flag doctor check: a claude seat WITHOUT --safe-mode is flagged
    sys.path.insert(0, os.path.join(ROOT, "bin"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("leos_doctor", os.path.join(ROOT, "bin", "leos-doctor.py"))
    doc = importlib.util.module_from_spec(spec); spec.loader.exec_module(doc)
    bad = doc.check_seat_flags("claude", {"host": "claude", "native": {"mode": "subagent", "model": "opus"}, "seats": [
        {"name": "opus", "provider": "anthropic", "transport": "stdin",
         "argv": ["claude", "--print", "--model", "opus", "--permission-mode", "plan"]}]})
    check("doctor flags claude seat missing --safe-mode", any("safe-mode" in p for p in bad))
    good = doc.check_seat_flags("claude", {"host": "claude", "native": {"mode": "subagent", "model": "opus"}, "seats": [
        {"name": "opus", "provider": "anthropic", "transport": "stdin",
         "argv": ["claude", "--safe-mode", "--print", "--no-session-persistence",
                  "--model", "opus", "--permission-mode", "plan"]}]})
    check("doctor passes schema-valid claude seat", not good)
    unresolved = doc.check_seat_flags("codex", {"host": "codex",
        "native": {"mode": "exec", "transport": "stdin",
                   "argv": ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "-"]},
        "seats": [{"name": "opus", "provider": "anthropic", "transport": "stdin",
                   "argv": ["claude", "--safe-mode", "--print", "--no-session-persistence",
                            "--permission-mode", "plan", "--model", "{MODEL}"]}]})
    check("doctor rejects unresolved model placeholders", any("unresolved" in p for p in unresolved))
    check("doctor detects owned destination drift",
          doc._owned_mismatches({"features": {"hooks": False}}, {"features": {"hooks": True}})
          == ["features.hooks"])

    # 18. doctor: instruction-delivery + retired-symlink + malformed-config checks (new logic).
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
        doc.check_tool("codex", True, probs, rep)
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
        else:
            os.environ.pop("HOME", None)

    # 19. Resolved seat files go through the private atomic writer; placeholders and missing
    # driver-smoke confirmations are refused.
    seat_local = tempfile.mkdtemp(prefix="seatlocal.")
    _cleanup.append(seat_local)
    candidate = os.path.join(seat_local, "candidate.json")
    with open(candidate, "w") as f:
        json.dump({"host": "codex", "native": {"mode": "exec", "transport": "stdin",
                   "argv": ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "-"]},
                   "seats": []}, f)
    seat_writer = os.path.join(ROOT, "bin", "leos-seats.py")
    seat_env = dict(os.environ, LEOS_LOCAL=seat_local)
    wr = subprocess.run([sys.executable, seat_writer, "write", "--host", "codex", "--input", candidate],
                        capture_output=True, text=True, env=seat_env)
    installed_seats = os.path.join(seat_local, "seats.codex.json")
    check("seat writer installs validated private config", wr.returncode == 0 and
          os.path.isfile(installed_seats) and os.stat(installed_seats).st_mode & 0o777 == 0o600)
    with open(candidate, "w") as f:
        json.dump({"host": "codex", "native": {"mode": "exec", "transport": "stdin",
                   "argv": ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "-"]},
                   "seats": [{"name": "opus", "transport": "stdin",
                              "argv": ["claude", "--safe-mode", "--print", "--no-session-persistence",
                                       "--permission-mode", "plan", "--model", "{MODEL}"]}]}, f)
    vr = subprocess.run([sys.executable, seat_writer, "validate", "--host", "codex", "--input", candidate],
                        capture_output=True, text=True, env=seat_env)
    check("seat writer refuses unresolved model slug", vr.returncode == 1 and "unresolved" in vr.stdout)
    with open(candidate, "w") as f:
        json.dump({"host": "codex", "native": {"mode": "exec", "transport": "stdin",
                   "argv": ["some-unknown-cli", "review", "-"]}, "seats": []}, f)
    vr = subprocess.run([sys.executable, seat_writer, "validate", "--host", "codex", "--input", candidate],
                        capture_output=True, text=True, env=seat_env)
    check("seat writer refuses an unknown adapter/binary", vr.returncode == 1 and "adapter" in vr.stdout)
    with open(candidate, "w") as f:
        json.dump({"host": "codex", "native": {"mode": "exec", "transport": "stdin", "cwd": "repo",
                   "argv": ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "-"]},
                   "seats": []}, f)
    vr = subprocess.run([sys.executable, seat_writer, "validate", "--host", "codex", "--input", candidate],
                        capture_output=True, text=True, env=seat_env)
    check("seat writer accepts the documented cwd values", vr.returncode == 0)
    with open(candidate, "w") as f:
        json.dump({"host": "codex", "native": {"mode": "exec", "transport": "stdin", "cwd": "elsewhere",
                   "argv": ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "-"]},
                   "seats": []}, f)
    vr = subprocess.run([sys.executable, seat_writer, "validate", "--host", "codex", "--input", candidate],
                        capture_output=True, text=True, env=seat_env)
    check("seat writer refuses an unknown cwd mode", vr.returncode == 1 and "cwd" in vr.stdout)

    total = passed + failed
    print(f"council-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    for d in _cleanup:
        shutil.rmtree(d, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
