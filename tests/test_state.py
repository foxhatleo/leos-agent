#!/usr/bin/env python3
"""Tests for scripts/state.py."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from multiprocessing.pool import ThreadPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PY = os.path.join(REPO, "plugins", "leo", "scripts", "state.py")

spec = importlib.util.spec_from_file_location("state", STATE_PY)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class TestDeepMerge(unittest.TestCase):
    def test_dict_recursive_merge(self):
        base = {"a": {"x": 1}}
        patch = {"a": {"y": 2}}
        self.assertEqual(state.deep_merge(base, patch), {"a": {"x": 1, "y": 2}})

    def test_list_union_in_order(self):
        self.assertEqual(state.deep_merge([1, 2], [3, 4]), [1, 2, 3, 4])

    def test_list_dedupe(self):
        merged = state.deep_merge({"reviewed": [13]}.get("reviewed"), [13])
        self.assertEqual(merged, [13])
        # simulate merging {"reviewed": [13]} twice
        base = {}
        base["reviewed"] = state.deep_merge(base.get("reviewed"), [13])
        base["reviewed"] = state.deep_merge(base.get("reviewed"), [13])
        self.assertEqual(base["reviewed"], [13])

    def test_list_of_dicts_dedupe_and_append(self):
        base = [{"id": 1}]
        patch = [{"id": 1}, {"id": 2}]
        self.assertEqual(state.deep_merge(base, patch), [{"id": 1}, {"id": 2}])

    def test_scalar_overwrite(self):
        self.assertEqual(state.deep_merge(1, 2), 2)
        self.assertEqual(state.deep_merge("a", "b"), "b")

    def test_dict_over_scalar_replaces(self):
        self.assertEqual(state.deep_merge(1, {"a": 1}), {"a": 1})


class TestLoad(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(state.load(os.path.join(tmp, "nope.json")), {})


class TestAtomicWrite(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "f.json")
            state.atomic_write(path, {"a": 1})
            self.assertEqual(state.load(path), {"a": 1})

    def test_no_tmp_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.json")
            state.atomic_write(path, {"a": 1})
            leftovers = [f for f in os.listdir(tmp) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [])


def run_cli(args, env):
    return subprocess.run(
        [sys.executable, STATE_PY] + args,
        capture_output=True, text=True, env=env,
    )


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        self.env["LEOS_AGENT_PATH"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_then_get_roundtrip(self):
        r = run_cli(["merge", "conc", "repo/one", '{"a": 1}'], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli(["get", "conc", "repo/one"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"a": 1})

    def test_get_missing_name_returns_empty(self):
        r = run_cli(["get", "nonexistent"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {})

    def test_get_with_repo_key_returns_subtree(self):
        run_cli(["merge", "n2", "repo/one", '{"a": 1}'], self.env)
        run_cli(["merge", "n2", "repo/two", '{"b": 2}'], self.env)
        r = run_cli(["get", "n2", "repo/one"], self.env)
        self.assertEqual(json.loads(r.stdout), {"a": 1})

    def test_path_prints_expected_location(self):
        r = run_cli(["path", "foo"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), os.path.join(self.tmp.name, "local", "foo.json"))

    def test_corrupt_file_exits_nonzero_with_message(self):
        local_dir = os.path.join(self.tmp.name, "local")
        os.makedirs(local_dir, exist_ok=True)
        with open(os.path.join(local_dir, "bad.json"), "w") as fh:
            fh.write("not json{{{")
        r = run_cli(["get", "bad"], self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("corrupt", r.stderr)

    def test_self_locate_without_env_override(self):
        """With LEOS_AGENT_PATH unset, state.py falls back to ~/.leos-agent
        (never to its own on-disk location, which may be a versioned plugin
        cache) — and auto-creates the local/ dir it resolves to."""
        with tempfile.TemporaryDirectory() as tmp_home:
            env = dict(os.environ)
            env.pop("LEOS_AGENT_PATH", None)
            env["HOME"] = tmp_home
            r = run_cli(["path", "foo"], env)
            self.assertEqual(r.returncode, 0, r.stderr)
            expected = os.path.join(tmp_home, ".leos-agent", "local", "foo.json")
            self.assertEqual(r.stdout.strip(), expected)
            self.assertTrue(os.path.isdir(os.path.join(tmp_home, ".leos-agent", "local")))


class TestConcurrency(unittest.TestCase):
    def test_concurrent_merges_no_lost_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["LEOS_AGENT_PATH"] = tmp

            def do_merge(n):
                return run_cli(["merge", "conc", "k", json.dumps({"ids": [n]})], env)

            n_calls = 40
            with ThreadPool(n_calls) as pool:
                results = pool.map(do_merge, range(n_calls))

            for r in results:
                self.assertEqual(r.returncode, 0, r.stderr)

            r = run_cli(["get", "conc", "k"], env)
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(r.stdout)
            self.assertEqual(sorted(data["ids"]), list(range(n_calls)))


if __name__ == "__main__":
    unittest.main()
