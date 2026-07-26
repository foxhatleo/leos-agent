"""Tests for hooks/cursor-guard.py — the Cursor beforeShellExecution adapter
over bash-guard.py's pure check(). Stdlib unittest only.

Run: python3 -m unittest tests.test_cursor_guard -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(REPO, "plugins", "leo", "hooks")
GUARD = os.path.join(HOOKS, "cursor-guard.py")
BASH_GUARD_PATH = os.path.join(HOOKS, "bash-guard.py")
HOME = os.path.realpath(os.path.expanduser("~"))


def _load_bash_guard():
    spec = importlib.util.spec_from_file_location("bash_guard_parity", BASH_GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command, cwd=None):
    payload = json.dumps({"command": command, "cwd": cwd})
    r = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True, text=True)
    return r.returncode, r.stdout


class TestCursorGuardDeny(unittest.TestCase):
    def test_rm_rf_home_is_denied(self):
        rc, stdout = run("rm -rf ~", cwd=HOME)
        self.assertEqual(rc, 0)  # cursor-guard always exits 0; verdict is in the JSON
        payload = json.loads(stdout)
        self.assertEqual(payload.get("permission"), "deny")


class TestCursorGuardAllow(unittest.TestCase):
    def test_ls_is_allowed(self):
        rc, stdout = run("ls", cwd=HOME)
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload.get("permission"), "allow")


class TestCursorGuardEdges(unittest.TestCase):
    def test_garbage_stdin_allowed(self):
        r = subprocess.run([sys.executable, GUARD], input="not json {{{", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        payload = json.loads(r.stdout)
        self.assertEqual(payload.get("permission"), "allow")

    def test_empty_command_allowed(self):
        rc, stdout = run("", cwd=HOME)
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload.get("permission"), "allow")


# PARITY: a small table drawn from test_guard.py's BLOCK/ALLOW cases, with the expected
# verdict hardcoded as a literal — NOT derived by calling bash_guard.check() at test time.
# A hardcoded table is essential here: if check() ever regressed to returning None for
# everything, a table built by calling check() would "expect" allow for every case and the
# test would stay green through the regression. cwd is fixed at $HOME/project for every case.
PARITY_CASES = (
    ("rm -rf ~", "deny"),
    ("rm -rf /", "deny"),
    ("rm -rf $UNRESOLVED_DEST", "deny"),
    ("sh -c 'rm -rf /'", "deny"),
    ('bash -c "rm -rf ~"', "deny"),
    ("rm -rf ~/project/node_modules", "allow"),
    ("rm -rf ./build", "allow"),
    ("git status", "allow"),
    ("git clean -n", "allow"),
)


class TestParityWithBashGuard(unittest.TestCase):
    def test_cursor_guard_matches_hardcoded_expected_verdicts(self):
        """Cursor-guard's verdict against a table of literal, hardcoded expectations — this
        assertion alone would catch check() regressing to always-allow, unlike a table
        derived from calling check() itself."""
        cwd = os.path.join(HOME, "project")
        for command, expected_permission in PARITY_CASES:
            with self.subTest(command=command):
                rc, stdout = run(command, cwd=cwd)
                self.assertEqual(rc, 0)
                payload = json.loads(stdout)
                self.assertEqual(payload.get("permission"), expected_permission)

    def test_cursor_guard_agrees_with_bash_guard_check(self):
        """Separately, confirm cursor-guard's verdict tracks bash-guard.py's own check() output
        (the delegation contract) — kept distinct from the hardcoded-table assertion above so
        neither check alone can mask a regression in the other."""
        bash_guard = _load_bash_guard()
        cwd = os.path.join(HOME, "project")
        for command, _ in PARITY_CASES:
            with self.subTest(command=command):
                reason = bash_guard.check(command, cwd)
                rc, stdout = run(command, cwd=cwd)
                self.assertEqual(rc, 0)
                payload = json.loads(stdout)
                expected_permission = "deny" if reason else "allow"
                self.assertEqual(payload.get("permission"), expected_permission)


if __name__ == "__main__":
    unittest.main()
