#!/usr/bin/env python3
"""Tests for bin/leos-merge.py — the JSON/TOML merge engine.

Runs the real tool via `--dest --fragment --strategy` against temp files in an isolated HOME +
isolated LEOS_LOCAL (so nothing touches the real clone). Covers: fresh merge, array union,
scalar preservation, retire-on-shrink, foreign-conflict refusal, forced override, TOML round-trip,
and no-op idempotence. Run: bin/leos-python tests/merge-tests.py
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TEST_TMP = os.path.join(ROOT, "local", "test-work")
os.makedirs(TEST_TMP, exist_ok=True)
tempfile.tempdir = TEST_TMP
MERGE = os.path.join(ROOT, "bin", "leos-merge.py")

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def run(env, dest, fragment, strategy, force=False):
    frag = os.path.join(env["_TMP"], "frag." + ("toml" if strategy == "merge-toml" else "json"))
    if strategy == "merge-toml":
        with open(frag, "w") as f:
            f.write(fragment)
    else:
        with open(frag, "w") as f:
            json.dump(fragment, f)
    args = [sys.executable, MERGE, "--dest", dest, "--fragment", frag, "--strategy", strategy]
    if force:
        args.append("--force")
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    try:
        out = json.loads(r.stdout)
    except Exception:
        out = [{"applied": False, "raw": r.stdout, "err": r.stderr}]
    return r.returncode, out[0]


def main():
    home = tempfile.mkdtemp(prefix="lmhome.")
    local = tempfile.mkdtemp(prefix="lmlocal.")
    env = dict(os.environ, HOME=home, LEOS_LOCAL=local, _TMP=local)
    dest = os.path.join(home, ".claude", "settings.json")

    # 1. fresh merge into a missing dest
    ec, r = run(env, "~/.claude/settings.json", {"permissions": {"deny": ["a", "b"]}}, "merge-json")
    check("fresh merge applies", r.get("applied") and ec == 0)
    cur = json.load(open(dest))
    check("fresh content written", cur["permissions"]["deny"] == ["a", "b"])

    # 2. array union (add c; keep a,b; machine-added z preserved)
    cur["permissions"]["deny"].append("z-machine")
    json.dump(cur, open(dest, "w"))
    ec, r = run(env, "~/.claude/settings.json", {"permissions": {"deny": ["a", "b", "c"]}}, "merge-json")
    cur = json.load(open(dest))
    check("array union adds c", "c" in cur["permissions"]["deny"])
    check("array union keeps machine value", "z-machine" in cur["permissions"]["deny"])

    # 3. no-op idempotence (re-merge same fragment -> no actions)
    ec, r = run(env, "~/.claude/settings.json", {"permissions": {"deny": ["a", "b", "c"]}}, "merge-json")
    check("no-op re-merge", r.get("applied") and r.get("actions") == [])

    # 4. retire-on-shrink: drop "c" from the fragment -> c retired, machine value kept
    ec, r = run(env, "~/.claude/settings.json", {"permissions": {"deny": ["a", "b"]}}, "merge-json")
    cur = json.load(open(dest))
    check("retire drops removed element", "c" not in cur["permissions"]["deny"])
    check("retire keeps machine element", "z-machine" in cur["permissions"]["deny"])

    # 5. foreign conflict: a scalar the user changed away from our value -> refuse
    ec, r = run(env, "~/.claude/settings.json", {"theme": "dark"}, "merge-json")   # set ours
    cur = json.load(open(dest)); cur["theme"] = "solarized"; json.dump(cur, open(dest, "w"))
    ec, r = run(env, "~/.claude/settings.json", {"theme": "light"}, "merge-json")
    check("foreign scalar conflict refused", (not r.get("applied")) and r.get("conflicts"))
    check("foreign value untouched on refuse", json.load(open(dest))["theme"] == "solarized")
    ec, r = run(env, "~/.claude/settings.json", {"theme": "light"}, "merge-json", force=True)
    check("forced override wins", json.load(open(dest))["theme"] == "light")

    # 6. TOML round-trip + unicode
    tdest = os.path.join(home, ".codex", "config.toml")
    ec, r = run(env, "~/.codex/config.toml", '[features]\nhooks = true\nname = "café ✅"\n', "merge-toml")
    check("toml merge applies", r.get("applied") and ec == 0)
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    with open(tdest, "rb") as f:
        td = tomllib.load(f)
    check("toml value round-trips", td["features"]["hooks"] is True and td["features"]["name"] == "café ✅")

    # 7. {{CLONE_ROOT}} token expansion: expanded in the written file + stored values, drift hash
    #    computed on the TEMPLATE (token), user's pre-existing entry preserved, re-merge idempotent.
    idest = os.path.join(home, ".config", "opencode", "opencode.json")
    os.makedirs(os.path.dirname(idest), exist_ok=True)
    json.dump({"instructions": ["docs/user.md"]}, open(idest, "w"))   # pre-existing user entry
    frag = {"instructions": ["{{CLONE_ROOT}}/global/AGENTS.md"]}
    ec, r = run(env, "~/.config/opencode/opencode.json", frag, "merge-json")
    cur = json.load(open(idest))
    expanded = os.path.join(ROOT, "global", "AGENTS.md")
    check("token expanded in written file", expanded in cur["instructions"])
    check("no raw token left in file", not any("{{CLONE_ROOT}}" in x for x in cur["instructions"]))
    check("array-union preserves user's instructions entry", "docs/user.md" in cur["instructions"])
    import hashlib
    state = json.load(open(os.path.join(local, "merge-state.json")))
    entry = state["merges"]["~/.config/opencode/opencode.json"]
    template_sha = hashlib.sha256(json.dumps(frag, sort_keys=True).encode("utf-8", "replace")).hexdigest()
    check("fragmentSha hashes the template (token, machine-independent)", entry["fragmentSha"] == template_sha)
    check("stored values hold the RESOLVED path", expanded in entry["values"]["instructions"])
    ec, r = run(env, "~/.config/opencode/opencode.json", frag, "merge-json")
    cur = json.load(open(idest))
    check("token re-merge idempotent (no duplicate)", cur["instructions"].count(expanded) == 1)

    # 8. Claude's selected package-manager rules are rendered by the merge tool, not left as an
    # undocumented manual setup edit.
    pmhome = tempfile.mkdtemp(prefix="pmhome.")
    pmlocal = tempfile.mkdtemp(prefix="pmlocal.")
    pmenv = dict(os.environ, HOME=pmhome, LEOS_LOCAL=pmlocal)
    pr = subprocess.run([sys.executable, MERGE, "--tool", "claude", "--package-manager", "pnpm"],
                        capture_output=True, text=True, env=pmenv)
    try:
        policy_settings = json.load(open(os.path.join(pmhome, ".claude", "settings.json")))
    except Exception:
        policy_settings = {}
    check("package-manager merge applies", pr.returncode == 0)
    check("package-manager allow is rendered", "Bash(pnpm test:*)" in
          policy_settings.get("permissions", {}).get("allow", []))
    pmstate = json.load(open(os.path.join(pmlocal, "merge-state.json")))
    check("package-manager policy fingerprint recorded", next(iter(pmstate["merges"].values())).get("packageManager") == "pnpm")
    # Doctor's ordinary upgrade command is intentionally allowed to omit --package-manager; the
    # merge state must preserve the established choice rather than retiring those allowances.
    rerun = subprocess.run([sys.executable, MERGE, "--tool", "claude"], capture_output=True, text=True, env=pmenv)
    retained = json.load(open(os.path.join(pmhome, ".claude", "settings.json")))
    check("ordinary Claude re-merge retains package-manager allow", rerun.returncode == 0 and
          "Bash(pnpm test:*)" in retained.get("permissions", {}).get("allow", []))
    import shutil
    shutil.rmtree(pmhome, ignore_errors=True)
    shutil.rmtree(pmlocal, ignore_errors=True)

    # 9. Each host map merges into its correct host-owned destination; Codex honors CODEX_HOME.
    host_home = tempfile.mkdtemp(prefix="hostmerge.")
    host_local = tempfile.mkdtemp(prefix="hostlocal.")
    codex_home = os.path.join(host_home, "relocated-codex")
    host_env = dict(os.environ, HOME=host_home, LEOS_LOCAL=host_local, CODEX_HOME=codex_home)
    codex_merge = subprocess.run([sys.executable, MERGE, "--tool", "codex"], capture_output=True, text=True, env=host_env)
    opencode_merge = subprocess.run([sys.executable, MERGE, "--tool", "opencode"], capture_output=True, text=True, env=host_env)
    cursor_merge = subprocess.run([sys.executable, MERGE, "--tool", "cursor"], capture_output=True, text=True, env=host_env)
    try:
        ocfg = json.load(open(os.path.join(host_home, ".config", "opencode", "opencode.json")))
        cursor_cfg = json.load(open(os.path.join(host_home, ".cursor", "cli-config.json")))
    except Exception:
        ocfg, cursor_cfg = {}, {}
    check("Codex merge honors CODEX_HOME", codex_merge.returncode == 0 and
          os.path.isfile(os.path.join(codex_home, "config.toml")))
    check("OpenCode merge writes additive instructions", opencode_merge.returncode == 0 and
          os.path.join(ROOT, "global", "AGENTS.md") in ocfg.get("instructions", []))
    check("Cursor merge writes its native permission schema", cursor_merge.returncode == 0 and
          "Read(**/.env)" in cursor_cfg.get("permissions", {}).get("deny", []))
    shutil.rmtree(host_home, ignore_errors=True)
    shutil.rmtree(host_local, ignore_errors=True)

    total = passed + failed
    print(f"merge-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    import shutil
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(local, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
