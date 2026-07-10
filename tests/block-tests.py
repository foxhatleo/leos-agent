#!/usr/bin/env python3
"""Tests for bin/leos-block.py — the managed @import block in Claude's CLAUDE.md.

Runs the real tool with `--tool claude` against an isolated HOME + LEOS_LOCAL (nothing touches the
real clone). Covers: fresh create, idempotence, coexistence with user content, stale-path refresh,
legacy-symlink migration, foreign-symlink follow, and --check exit codes.
Run: bin/leos-python tests/block-tests.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TEST_TMP = os.path.join(ROOT, "local", "test-work")
os.makedirs(TEST_TMP, exist_ok=True)
tempfile.tempdir = TEST_TMP
BLOCK = os.path.join(ROOT, "bin", "leos-block.py")
IMPORT_ABS = os.path.join(ROOT, "global", "AGENTS.md")
MARKER = "leos-agent:global-instructions"
BEGIN = f"<!-- {MARKER} BEGIN -->"

passed = failed = 0
_cleanup = []


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def fresh():
    home = tempfile.mkdtemp(prefix="blockhome.")
    local = tempfile.mkdtemp(prefix="blocklocal.")
    _cleanup.extend([home, local])
    return home, local


def run(home, local, check_mode=False):
    env = dict(os.environ, HOME=home, LEOS_LOCAL=local)
    args = [sys.executable, BLOCK, "--tool", "claude"]
    if check_mode:
        args.append("--check")
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    return r.returncode


def cpath(home):
    return os.path.join(home, ".claude", "CLAUDE.md")


def read(home):
    with open(cpath(home), encoding="utf-8") as f:
        return f.read()


def main():
    # 1. fresh create
    home, local = fresh()
    ec = run(home, local)
    txt = read(home)
    check("fresh create exit 0", ec == 0)
    check("fresh create writes the block", BEGIN in txt and f"@{IMPORT_ABS}" in txt)

    # 2. idempotent re-run -> exactly one block
    run(home, local)
    check("idempotent: exactly one block", read(home).count(BEGIN) == 1)

    # 3. coexistence: user content preserved, block appended
    home, local = fresh()
    os.makedirs(os.path.dirname(cpath(home)))
    with open(cpath(home), "w") as f:
        f.write("# My rules\nalways lint\n")
    run(home, local)
    txt = read(home)
    check("coexist: user content preserved", "always lint" in txt)
    check("coexist: block appended", BEGIN in txt and f"@{IMPORT_ABS}" in txt)

    # 4. stale-path refresh: a block with the wrong @path is corrected in place
    home, local = fresh()
    os.makedirs(os.path.dirname(cpath(home)))
    with open(cpath(home), "w") as f:
        f.write(f"# rules\n\n{BEGIN}\n@/old/stale/path.md\n<!-- {MARKER} END -->\n")
    run(home, local)
    txt = read(home)
    check("stale path refreshed to real import", f"@{IMPORT_ABS}" in txt and "/old/stale/path.md" not in txt)
    check("stale refresh keeps a single block", txt.count(BEGIN) == 1)

    # 5. legacy bare symlink INTO the clone -> migrated to a real file (clone file untouched)
    home, local = fresh()
    os.makedirs(os.path.dirname(cpath(home)))
    os.symlink(IMPORT_ABS, cpath(home))
    clone_before = open(IMPORT_ABS, encoding="utf-8").read()
    run(home, local)
    check("legacy symlink migrated to real file", not os.path.islink(cpath(home)))
    check("migrated file carries the block", BEGIN in read(home) and f"@{IMPORT_ABS}" in read(home))
    check("clone global/AGENTS.md untouched by migration",
          open(IMPORT_ABS, encoding="utf-8").read() == clone_before)

    # 6. foreign symlink -> block ensured in the real target, symlink preserved
    home, local = fresh()
    os.makedirs(os.path.dirname(cpath(home)))
    real_target = os.path.join(home, "dotfiles-claude.md")
    with open(real_target, "w") as f:
        f.write("# dotfiles claude\nmy prefs\n")
    os.symlink(real_target, cpath(home))
    run(home, local)
    check("foreign symlink preserved", os.path.islink(cpath(home)))
    tgt = open(real_target, encoding="utf-8").read()
    check("foreign target keeps user content + gets block", "my prefs" in tgt and BEGIN in tgt)

    # 7. --check exit codes
    home, local = fresh()
    check("--check on missing exits 1", run(home, local, check_mode=True) == 1)
    run(home, local)
    check("--check on present exits 0", run(home, local, check_mode=True) == 0)

    total = passed + failed
    print(f"block-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    for d in _cleanup:
        shutil.rmtree(d, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
