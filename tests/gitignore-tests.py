#!/usr/bin/env python3
"""Tests for bin/leos-gitignore.py using isolated HOME and global Git config files."""

import atexit
import os
import glob
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TEST_TMP = os.path.join(ROOT, "local", "test-work")
os.makedirs(TEST_TMP, exist_ok=True)
tempfile.tempdir = TEST_TMP
TOOL = os.path.join(ROOT, "bin", "leos-gitignore.py")


def _cleanup_tempdirs():
    """Remove this battery's tempdirs so repeated runs don't accumulate under local/test-work."""
    for d in glob.glob(os.path.join(TEST_TMP, "gitignore-*")):
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup_tempdirs)

passed = failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def fixture():
    home = tempfile.mkdtemp(prefix="gitignore-home.")
    local = tempfile.mkdtemp(prefix="gitignore-local.")
    config = os.path.join(home, ".gitconfig-test")
    env = dict(os.environ, HOME=home, LEOS_LOCAL=local, GIT_CONFIG_GLOBAL=config,
               GIT_CONFIG_NOSYSTEM="1")
    return home, local, config, env


def run(env):
    return subprocess.run([sys.executable, TOOL], env=env, capture_output=True, text=True)


def git_get(env):
    return subprocess.run(["git", "config", "--global", "--get", "core.excludesFile"],
                          env=env, capture_output=True, text=True)


def main():
    home, local, config, env = fixture()
    result = run(env)
    excludes = os.path.join(home, ".gitignore")
    text = open(excludes, encoding="utf-8").read()
    check("unset config succeeds", result.returncode == 0)
    check("unset config selects ~/.gitignore", git_get(env).stdout.strip() == "~/.gitignore")
    check("fresh file contains both entries", text == ".council-off\n.council.json\n")

    before = open(config, "rb").read(), open(excludes, "rb").read()
    result = run(env)
    after = open(config, "rb").read(), open(excludes, "rb").read()
    check("repeat is idempotent", result.returncode == 0 and before == after)

    home, local, config, env = fixture()
    custom = os.path.join(home, "global-ignore")
    with open(custom, "w", encoding="utf-8") as f:
        f.write("*.cache\n.council-off\n")
    subprocess.run(["git", "config", "--global", "core.excludesFile", custom], env=env, check=True)
    result = run(env)
    text = open(custom, encoding="utf-8").read()
    check("configured file is reused", result.returncode == 0 and not os.path.exists(os.path.join(home, ".gitignore")))
    check("only missing entry appended", text == "*.cache\n.council-off\n.council.json\n")
    backups = []
    for base, _, files in os.walk(os.path.join(local, "backups")):
        backups.extend(os.path.join(base, name) for name in files)
    check("existing excludes file backed up", len(backups) == 1 and open(backups[0]).read() == "*.cache\n.council-off\n")

    home, _, _, env = fixture()
    outside = tempfile.mkdtemp(prefix="gitignore-outside.")
    subprocess.run(["git", "config", "--global", "core.excludesFile", outside], env=env, check=True)
    result = run(env)
    check("directory destination refused", result.returncode == 1)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
