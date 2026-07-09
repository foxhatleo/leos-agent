#!/usr/bin/env python3
"""Tests for core/hooks/format-on-edit.py — the PostToolUse format/lint hook.

Uses fake executable shims (no real toolchains needed) inside an isolated project under a temp
HOME. Covers: apply_patch + file_path path extraction, JS eslint --fix + lint feedback (exit 44),
Python ruff, a clean pass (exit 0), and the home-boundary no-op. Run: python3 tests/fmt-tests.py
"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
FMT = os.path.join(ROOT, "core", "hooks", "format-on-edit.py")

spec = importlib.util.spec_from_file_location("fmt", FMT)
fmt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fmt)

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def shim(binpath, exit_code, stderr=""):
    with open(binpath, "w") as f:
        f.write(f"#!/bin/sh\n[ -n \"{stderr}\" ] && echo '{stderr}' >&2\nexit {exit_code}\n")
    os.chmod(binpath, os.stat(binpath).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def run_hook(home, payload):
    r = subprocess.run(["python3", FMT], input=json.dumps(payload), capture_output=True, text=True,
                       env=dict(os.environ, HOME=home, PATH=os.path.join(home, "bin") + ":" + os.environ["PATH"]))
    return r.returncode, r.stderr


def main():
    # unit: edited_paths parses both payload shapes
    ap = fmt.edited_paths({"tool_name": "apply_patch",
                           "tool_input": {"command": "*** Update File: src/x.ts\n@@\n+a"},
                           "cwd": "/tmp/proj"})
    check("apply_patch path parsed", ap == ["/tmp/proj/src/x.ts"])
    fpp = fmt.edited_paths({"tool_name": "Edit", "tool_input": {"file_path": "/tmp/proj/y.py"}})
    check("file_path parsed", fpp == ["/tmp/proj/y.py"])

    home = tempfile.mkdtemp(prefix="fmthome.")
    os.makedirs(os.path.join(home, "bin"))
    proj = os.path.join(home, "proj")
    os.makedirs(proj)

    # JS project with an eslint config + an eslint shim that reports a lint error
    with open(os.path.join(proj, "eslint.config.js"), "w") as f:
        f.write("export default [];\n")
    jsfile = os.path.join(proj, "a.js")
    with open(jsfile, "w") as f:
        f.write("const x = 1\n")
    shim(os.path.join(home, "bin", "eslint"), 1, stderr="a.js:1:1 error no-unused-vars")
    ec, err = run_hook(home, {"tool_name": "Edit", "tool_input": {"file_path": jsfile}})
    check("eslint lint feedback -> exit 44", ec == 44)
    check("feedback mentions the tool", "eslint" in err)

    # clean pass: eslint exits 0 -> silent (exit 0)
    shim(os.path.join(home, "bin", "eslint"), 0)
    ec, err = run_hook(home, {"tool_name": "Edit", "tool_input": {"file_path": jsfile}})
    check("clean eslint -> exit 0", ec == 0)

    # Python project with ruff config + ruff shim (format ok, check reports)
    with open(os.path.join(proj, "ruff.toml"), "w") as f:
        f.write("line-length = 100\n")
    pyfile = os.path.join(proj, "b.py")
    with open(pyfile, "w") as f:
        f.write("x=1\n")
    shim(os.path.join(home, "bin", "ruff"), 1, stderr="b.py:1:1 F401")
    ec, err = run_hook(home, {"tool_name": "Edit", "tool_input": {"file_path": pyfile}})
    check("ruff feedback -> exit 44", ec == 44)

    # unknown extension -> no-op (exit 0)
    other = os.path.join(proj, "c.txt")
    with open(other, "w") as f:
        f.write("hi\n")
    ec, err = run_hook(home, {"tool_name": "Edit", "tool_input": {"file_path": other}})
    check("non-code file -> exit 0", ec == 0)

    # file outside HOME -> never formatted (exit 0)
    outside = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
    outside.write(b"x=1\n"); outside.close()
    ec, err = run_hook(home, {"tool_name": "Edit", "tool_input": {"file_path": outside.name}})
    check("file outside HOME -> exit 0", ec == 0)
    os.unlink(outside.name)

    total = passed + failed
    print(f"fmt-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    import shutil
    shutil.rmtree(home, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
