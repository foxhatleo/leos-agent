"""Handoff names cannot escape storage; concurrent creation reserves paths."""
import concurrent.futures
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "handoff.py"


class HandoffSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, LEOS_AGENT_LOCAL_PATH=str(self.root / "state"))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True)

    def test_traversal_and_absolute_names_cannot_delete(self):
        victim = self.root / "victim.md"
        victim.write_text("keep")
        for name in ("../../victim", str(victim.with_suffix("")), "", "../"):
            self.assertNotEqual(self.run_cli("rm", name).returncode, 0)
            self.assertEqual(victim.read_text(), "keep")

    def test_symlinks_are_neither_resolved_nor_listed(self):
        self.run_cli("new", "ordinary")
        victim = self.root / "victim.md"
        victim.write_text("private")
        link = self.root / "state/handoffs/linked.md"
        link.symlink_to(victim)
        self.assertNotEqual(self.run_cli("path", "linked").returncode, 0)
        self.assertNotEqual(self.run_cli("rm", "linked").returncode, 0)
        self.assertNotIn("linked", self.run_cli("list", "--all").stdout)
        self.assertEqual(victim.read_text(), "private")

    def test_parallel_creation_reserves_unique_private_files(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.run_cli("new", "same-name"), range(16)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        paths = [Path(r.stdout.splitlines()[1]) for r in results]
        self.assertEqual(len(set(paths)), 16)
        self.assertTrue(all(p.is_file() and p.stat().st_mode & 0o777 == 0o600 for p in paths))

    def test_symlink_directory_is_refused(self):
        (self.root / "state").mkdir()
        (self.root / "external").mkdir()
        (self.root / "state/handoffs").symlink_to(self.root / "external", target_is_directory=True)
        self.assertNotEqual(self.run_cli("new", "outside").returncode, 0)
        self.assertEqual(list((self.root / "external").iterdir()), [])
