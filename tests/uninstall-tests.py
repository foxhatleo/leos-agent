#!/usr/bin/env python3
"""End-to-end ownership-safe host uninstall regression."""

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
passed = failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


def main():
    root = tempfile.mkdtemp(prefix="uninstall.")
    home, local = os.path.join(root, "home"), os.path.join(root, "local")
    os.makedirs(os.path.join(home, ".claude")); os.makedirs(local)
    env = dict(os.environ, HOME=home, LEOS_LOCAL=local)
    claude_md = os.path.join(home, ".claude", "CLAUDE.md")
    with open(claude_md, "w") as f:
        f.write("# User instructions\nkeep me\n")
    settings = os.path.join(home, ".claude", "settings.json")
    with open(settings, "w") as f:
        json.dump({"userOwned": True}, f)
    commands = [
        [sys.executable, os.path.join(ROOT, "bin", "leos-link.py"), "--tool", "claude"],
        [sys.executable, os.path.join(ROOT, "bin", "leos-merge.py"), "--tool", "claude"],
        [sys.executable, os.path.join(ROOT, "bin", "leos-block.py"), "--tool", "claude"],
    ]
    setup_ok = all(subprocess.run(cmd, env=env, capture_output=True, text=True).returncode == 0
                   for cmd in commands)
    check("fixture host setup succeeds", setup_ok)
    with open(os.path.join(local, "seats.claude.json"), "w") as f:
        json.dump({"host": "claude"}, f)
    uninstall = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "leos-uninstall.py"),
                                "--tool", "claude"], env=env, capture_output=True, text=True)
    check("uninstall succeeds", uninstall.returncode == 0)
    resulting_settings = json.load(open(settings))
    resulting_instructions = open(claude_md).read()
    check("uninstall preserves foreign settings", resulting_settings == {"userOwned": True})
    check("uninstall preserves user instructions and removes managed block",
          "keep me" in resulting_instructions and "leos-agent:global-instructions" not in resulting_instructions)
    check("uninstall removes Leo symlinks", not os.path.lexists(os.path.join(home, ".claude", "leos-python")))
    check("uninstall removes machine-local host seats", not os.path.exists(os.path.join(local, "seats.claude.json")))
    registry = json.load(open(os.path.join(local, "installed-hosts.json")))
    check("uninstall updates host registry", "claude" not in registry.get("hosts", []))
    shutil.rmtree(root, ignore_errors=True)
    total = passed + failed
    print(f"uninstall-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
