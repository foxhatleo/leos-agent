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


def fixture(prefix):
    """Isolated HOME + LEOS_LOCAL; the real private venv is symlinked into the isolated local so
    chained bin/leos-python invocations (uninstall -> merge/block) keep working under LEOS_LOCAL."""
    root = tempfile.mkdtemp(prefix=prefix)
    home, local = os.path.join(root, "home"), os.path.join(root, "local")
    os.makedirs(home); os.makedirs(local)
    real_venv = os.path.join(ROOT, "local", ".venv")
    if os.path.isdir(real_venv):
        os.symlink(real_venv, os.path.join(local, ".venv"))
    return root, home, local, dict(os.environ, HOME=home, LEOS_LOCAL=local)


def main():
    root, home, local, env = fixture("uninstall.")
    os.makedirs(os.path.join(home, ".claude"))
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

    # Shared-link protection must rest on physical evidence, not just the registry: install
    # codex + opencode, lose opencode's registry entry, and the shared skills dir must survive.
    root2, home2, local2, env2 = fixture("uninstall2.")
    for tool in ("codex", "opencode"):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "leos-link.py"), "--tool", tool],
                           env=env2, capture_output=True, text=True)
        check(f"fixture links {tool}", r.returncode == 0)
    with open(os.path.join(local2, "installed-hosts.json"), "w") as f:
        json.dump({"hosts": ["codex"]}, f)   # opencode registration lost
    shared_skills = os.path.join(home2, ".agents", "skills", "council")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "leos-uninstall.py"),
                        "--tool", "codex"], env=env2, capture_output=True, text=True)
    check("uninstall with lost registry succeeds", r.returncode == 0)
    check("shared skills link survives via live-link evidence", os.path.islink(shared_skills))
    check("codex-only link is removed", not os.path.lexists(os.path.join(home2, ".codex", "leos-python")))
    check("opencode links untouched",
          os.path.islink(os.path.join(home2, ".config", "opencode", "leos-python")))
    shutil.rmtree(root2, ignore_errors=True)

    # Removing the genuinely last host must still remove the shared link (no permanent false-keep),
    # and user keys added inside a Leo-owned dict must survive the full uninstall path.
    root3, home3, local3, env3 = fixture("uninstall3.")
    for cmd in (["leos-link.py"], ["leos-merge.py"]):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", cmd[0]), "--tool", "codex"],
                           env=env3, capture_output=True, text=True)
        check(f"fixture {cmd[0]} codex succeeds", r.returncode == 0)
    hooks_path = os.path.join(home3, ".codex", "hooks.json")
    hooks = json.load(open(hooks_path))
    hooks["hooks"]["SessionEnd"] = [{"userCustom": True}]   # user key INSIDE Leo's dict
    json.dump(hooks, open(hooks_path, "w"))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "leos-uninstall.py"),
                        "--tool", "codex"], env=env3, capture_output=True, text=True)
    check("last-host uninstall succeeds with mixed hook dict", r.returncode == 0)
    check("last host removes the shared skills link",
          not os.path.lexists(os.path.join(home3, ".agents", "skills", "council")))
    left_hooks = json.load(open(hooks_path))
    check("user hook group inside Leo's dict survives uninstall",
          left_hooks.get("hooks", {}).get("SessionEnd") == [{"userCustom": True}])
    check("Leo hook groups are retired", "PreToolUse" not in left_hooks.get("hooks", {}))
    shutil.rmtree(root3, ignore_errors=True)

    # Registry robustness: a hosts-less dict or a non-dict registry must not crash link/uninstall.
    root4, home4, local4, env4 = fixture("uninstall4.")
    with open(os.path.join(local4, "installed-hosts.json"), "w") as f:
        f.write("{}")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "leos-link.py"), "--tool", "claude"],
                       env=env4, capture_output=True, text=True)
    check("link tolerates a hosts-less registry", r.returncode == 0)
    with open(os.path.join(local4, "installed-hosts.json"), "w") as f:
        f.write("[]")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "leos-uninstall.py"),
                        "--tool", "claude"], env=env4, capture_output=True, text=True)
    check("uninstall tolerates a non-dict registry", r.returncode == 0)
    shutil.rmtree(root4, ignore_errors=True)

    total = passed + failed
    print(f"uninstall-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
