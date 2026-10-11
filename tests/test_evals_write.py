"""The write-capable eval suite under evals-write/ grades what it says it grades.

Its cases edit files and run tests, so they need an operator grant
(`--allow-tools`), and live outside evals/ so the read-only suite's discovery
(`evals/**/case.yaml`) never loads them and needs no grant. Every run is a paid
model call; this module is the suite's offline gate. It stages each case the
way the runner does, applies a reference change, runs the case's own check,
and feeds the real test output to the graders in the shape a run trace has.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench  # noqa: E402
import test_evals as ev  # noqa: E402

WRITE = ROOT / "evals-write"
GATED = {"Edit", "Write", "Bash"}
FILE_TARGET_KEYS = {"source", "path"}
_SANDBOX = None
_ENV = None


def setUpModule():
    global _SANDBOX, _ENV
    _SANDBOX = tempfile.TemporaryDirectory()
    env = {k: v for k, v in os.environ.items() if k not in ev._CONFIG_ENV}
    env.update(HOME=_SANDBOX.name, LEOS_AGENT_LOCAL_PATH=os.path.join(_SANDBOX.name, "local"),
               LEOS_AGENT_PRICE_REFRESH="off")
    _ENV = mock.patch.dict(os.environ, env, clear=True)
    _ENV.start()


def tearDownModule():
    _ENV.stop()
    _SANDBOX.cleanup()


def run_env(home):
    """What a run's shell and the scaffold get: a fresh home, no config."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "TERM": "dumb",
            "PYTHONDONTWRITEBYTECODE": "1"}


def stage(case_dir, workspace, home):
    done = subprocess.run(["bash", str((case_dir / ev.SCAFFOLD).resolve())], cwd=workspace, env=run_env(home),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise AssertionError(done.stderr)


def check(case_dir, workspace, home):
    """The case's named check, as the prompt states it. Returns (ok, output)."""
    command = re.search(r"`(python3 -m unittest[^`]*)`", ev.load_suite(WRITE)[case_dir.name]["body"]).group(1)
    done = subprocess.run(command.split(), cwd=workspace, env=run_env(home), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)
    return done.returncode == 0, done.stdout + done.stderr


REFERENCE_TEST = '''import unittest

from shop.backoff import retry_delays


class Backoff(unittest.TestCase):
    def test_two_delays(self):
        self.assertEqual(retry_delays(2), [100, 200])
'''


def reference_change(workspace, rename=True, implement=True, own_test=True):
    shop = workspace / "shop"
    if rename:
        for path in shop.glob("*.py"):
            path.write_text(path.read_text(encoding="utf-8").replace("MAX_RETRIES", "RETRY_LIMIT"), encoding="utf-8")
    if implement:
        path = shop / "backoff.py"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "    raise NotImplementedError\n",
            "    return [min(base_ms * 2 ** i, cap_ms) for i in range(limit)]\n"), encoding="utf-8")
    if own_test:
        (workspace / "tests" / "test_backoff.py").write_text(REFERENCE_TEST, encoding="utf-8")


def trace_with(output):
    """A run trace whose Bash call returned `output`, one JSON message per line."""
    return "\n".join([
        ev.js_json({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_01", "name": "Bash",
             "input": {"command": "python3 -m unittest discover -s tests"}}]}}),
        ev.js_json({"type": "user", "message": {"role": "user", "content": [
            {"tool_use_id": "toolu_01", "type": "tool_result", "content": output}]}}),
    ])


def file_grader_passes(grader, workspace):
    path = workspace / grader["target"]["path"]
    if not path.is_file():
        return False  # the runner's grader throws, which scores as a failure
    return ev.regex_passes(grader, path.read_text(encoding="utf-8"))


class WriteSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = ev.load_suite(WRITE)

    def test_the_suite_sits_outside_the_read_only_one(self):
        self.assertTrue(self.cases)
        self.assertNotEqual(WRITE.resolve().parent, ev.EVALS.resolve())
        self.assertFalse(str(WRITE.resolve()).startswith(str(ev.EVALS.resolve()) + os.sep))
        self.assertIn("results/", (WRITE / ".gitignore").read_text(encoding="utf-8").split())
        self.assertIn(WRITE.name, bench.GRANTS)
        self.assertNotIn(bench.EVAL_DIR, bench.GRANTS)

    def test_case_format_and_the_grant_covers_exactly_the_gated_tools(self):
        grant = bench.GRANTS[WRITE.name]
        granted = {re.match(r"[A-Za-z]+", tool).group(0) for tool in grant}
        # Bash only with a command pattern, never the whole shell.
        self.assertTrue(all(tool != "Bash" and "*" not in tool or re.fullmatch(r"Bash\([^*]+:\*\)", tool)
                            for tool in grant), grant)
        for name, case in self.cases.items():
            with self.subTest(case=name):
                meta, data = case["meta"], case["case"]
                self.assertLessEqual(set(meta), ev.PROMPT_KEYS)
                self.assertLessEqual(set(data), ev.CASE_KEYS)
                self.assertEqual(data["name"], name)
                self.assertEqual(meta["model"], "opus")
                self.assertEqual(data["context"], {"scaffold_script": ev.SCAFFOLD})
                for rel in data["plugins"]:
                    self.assertEqual((case["dir"] / rel).resolve(), ROOT)
                self.assertTrue(all(ev.ENV_KEY_RE.fullmatch(k) for k in data["execution"]["env"]))
                tools = set(meta["allowed_tools"])
                self.assertLessEqual(tools, ev.READ_ONLY_TOOLS | GATED)
                self.assertEqual(tools & GATED, granted)
                # The prompt's check is a command the Bash grant allows.
                command = re.search(r"`(python3 -m unittest[^`]*)`", case["body"]).group(1)
                prefixes = [re.fullmatch(r"Bash\((.+):\*\)", t).group(1) for t in grant if t.startswith("Bash(")]
                self.assertTrue(any(command.startswith(p) for p in prefixes), command)

    def test_graders_are_documented_free_and_scored_in_both_arms(self):
        for name, case in self.cases.items():
            for grader_name, (grader, body) in case["graders"].items():
                with self.subTest(case=name, grader=grader_name):
                    self.assertEqual(grader["type"], "regex")
                    self.assertLessEqual(set(grader), ev.GRADER_KEYS | ev.TYPE_KEYS["regex"])
                    target = grader.get("target", "last_message")
                    if isinstance(target, dict):
                        self.assertEqual(set(target), FILE_TARGET_KEYS)
                        self.assertEqual(target["source"], "file")
                        self.assertFalse(Path(target["path"]).is_absolute() or ".." in Path(target["path"]).parts)
                    else:
                        self.assertIn(target, ev.TARGETS)
                    self.assertNotIn("arm", grader)
                    self.assertTrue(ev.scored_in_both_arms(grader))
                    pattern = grader["pattern"]
                    re.compile(pattern)
                    for construct, why in ev.FORBIDDEN:
                        self.assertIsNone(construct.search(pattern), why)
                    self.assertEqual(body.strip(), "")

    def test_staging_copies_writable_fixtures_and_a_read_only_contract(self):
        for name, case in self.cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                workspace, home = Path(tmp, "work"), Path(tmp, "home")
                workspace.mkdir()
                home.mkdir()
                stage(case["dir"], workspace, home)
                staged = sorted(p.name for p in workspace.iterdir())
                self.assertEqual(staged, ev.fixture_dirs(case["dir"]))
                for rel in staged:
                    self.assertEqual(ev.tree(workspace / rel), ev.tree(case["dir"] / rel))
                    self.assertIn(f"`{rel}`", case["body"])
                for path in (p for p in workspace.rglob("*") if p.is_file()):
                    writable = bool(path.stat().st_mode & 0o200)
                    self.assertEqual(writable, path.name != "test_contract.py", path)


class SeparableChange(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = ev.load_suite(WRITE)["separable-change"]
        cls.graders = {k: v[0] for k, v in cls.case["graders"].items()}

    def staged(self, tmp):
        workspace, home = Path(tmp, "work"), Path(tmp, "home")
        workspace.mkdir()
        home.mkdir()
        stage(self.case["dir"], workspace, home)
        return workspace, home

    def verdicts(self, workspace, output):
        return {"tests-pass": ev.regex_passes(self.graders["tests-pass"], trace_with(output)),
                "own-test": file_grader_passes(self.graders["own-test"], workspace),
                "contract-intact": file_grader_passes(self.graders["contract-intact"], workspace)}

    def test_the_reference_change_passes_its_check_and_every_grader(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, home = self.staged(tmp)
            reference_change(workspace)
            ok, output = check(self.case["dir"], workspace, home)
            self.assertTrue(ok, output)
            self.assertEqual(self.verdicts(workspace, output),
                             {"tests-pass": True, "own-test": True, "contract-intact": True})

    def test_unchanged_or_half_done_work_fails(self):
        for partial in ({"rename": False, "implement": False, "own_test": False},
                        {"rename": True, "implement": False, "own_test": True},
                        {"rename": False, "implement": True, "own_test": True}):
            with self.subTest(**partial), tempfile.TemporaryDirectory() as tmp:
                workspace, home = self.staged(tmp)
                reference_change(workspace, **partial)
                ok, output = check(self.case["dir"], workspace, home)
                self.assertFalse(ok)
                self.assertFalse(self.verdicts(workspace, output)["tests-pass"], output)

    def test_passing_without_a_test_of_its_own_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, home = self.staged(tmp)
            reference_change(workspace, own_test=False)
            ok, output = check(self.case["dir"], workspace, home)
            self.assertTrue(ok, output)  # the contract alone passes, but with seven tests
            verdicts = self.verdicts(workspace, output)
            self.assertFalse(verdicts["tests-pass"])
            self.assertFalse(verdicts["own-test"])

    def test_a_weakened_contract_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, home = self.staged(tmp)
            reference_change(workspace)
            contract = workspace / "tests" / "test_contract.py"
            os.chmod(contract, 0o644)
            text = contract.read_text(encoding="utf-8")
            contract.write_text(re.sub(r"    def test_caps_each_delay.*?\n\n", "", text, flags=re.DOTALL),
                                encoding="utf-8")
            self.assertFalse(self.verdicts(workspace, "")["contract-intact"])

    def test_the_trace_grader_reads_real_output_not_a_failure_or_a_mention(self):
        grader = self.graders["tests-pass"]
        self.assertTrue(ev.regex_passes(grader, trace_with("....\n----\nRan 12 tests in 0.004s\n\nOK\n")))
        self.assertTrue(ev.regex_passes(grader, trace_with("Ran 8 tests in 0.1s\n\nOK (skipped=1)\n")))
        for output in ("Ran 8 tests in 0.004s\n\nFAILED (failures=1)\n", "Ran 7 tests in 0.004s\n\nOK\n",
                       "Ran 0 tests in 0.000s\n\nOK\n"):
            self.assertFalse(ev.regex_passes(grader, trace_with(output)), output)


if __name__ == "__main__":
    unittest.main()
