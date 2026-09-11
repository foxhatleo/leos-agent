"""Behavioral tests for the date-based version bump.

The scheme is 12.YYYYMMDDXX.0 -- major 12, the UTC date with a two-digit
serial appended as the minor, patch always 0. The serial is what makes a second
release on the same day possible, and the reason it must stay one digit is the
ordering inversion these tests pin down.
"""

import datetime
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent

# Every file bump.py is responsible for keeping in step.
VERSIONED = (
    "package.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    "plugin.yaml",
    "README.md",
)


def load_bump():
    spec = importlib.util.spec_from_file_location("bump_test", ROOT / "scripts" / "bump.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pinned(module, year, month, day):
    return mock.patch.object(module, "today", lambda: datetime.date(year, month, day))


class BumpFixture(unittest.TestCase):
    """Each test works on a copy of the real manifests, never the working tree."""

    @classmethod
    def setUpClass(cls):
        cls.bump = load_bump()

    # Pinned, not inherited. Copying the manifests and bumping from whatever the
    # repo happens to be at made these tests pass or fail depending on whether
    # today's release had already been cut -- they went green in the morning and
    # red after the first commit of the day.
    STALE = "9.9.9"

    def fixture(self, stack):
        import json

        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        real = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        for rel in VERSIONED:
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                (ROOT / rel).read_text(encoding="utf-8").replace(real, self.STALE),
                encoding="utf-8",
            )
        return tmp

    def versions_in(self, root):
        import re

        found = set()
        for rel in VERSIONED:
            found |= set(re.findall(r"\b\d+\.\d+\.\d+\b", (root / rel).read_text(encoding="utf-8")))
        return found


class TestNextVersion(BumpFixture):
    def test_a_fresh_day_starts_the_serial_at_zero(self):
        with pinned(self.bump, 2026, 9, 7):
            self.assertEqual(self.bump.next_version("10.7.2"), "12.2026090700.0")

    def test_same_day_increments_the_serial(self):
        with pinned(self.bump, 2026, 9, 7):
            self.assertEqual(self.bump.next_version("12.2026090700.0"), "12.2026090701.0")
            self.assertEqual(self.bump.next_version("12.2026090798.0"), "12.2026090799.0")

    def test_a_new_day_resets_the_serial(self):
        with pinned(self.bump, 2026, 9, 8):
            self.assertEqual(self.bump.next_version("12.2026090705.0"), "12.2026090800.0")

    def test_a_101st_release_in_one_day_is_refused(self):
        # minor 2026090710 would sort ABOVE the next day's 202609080, silently
        # inverting version order. Refusing is the whole reason this guard runs.
        with pinned(self.bump, 2026, 9, 7):
            with self.assertRaises(self.bump.BumpError) as caught:
                self.bump.next_version("12.2026090799.0")
        self.assertIn("100 releases", str(caught.exception))

    def test_the_serial_stays_ordered_across_a_day_boundary(self):
        with pinned(self.bump, 2026, 9, 7):
            last_today = self.bump.next_version("12.2026090798.0")
        with pinned(self.bump, 2026, 9, 8):
            first_tomorrow = self.bump.next_version(last_today)
        minor = lambda v: int(v.split(".")[1])
        self.assertLess(minor(last_today), minor(first_tomorrow))


class TestRewrite(BumpFixture):
    def test_every_versioned_file_is_rewritten(self):
        import contextlib

        with contextlib.ExitStack() as stack:
            root = self.fixture(stack)
            old = self.bump.current_version(root)
            self.assertEqual(old, self.STALE)
            with pinned(self.bump, 2026, 9, 7):
                changed = self.bump.rewrite_all(root, old, "12.2026090700.0")
            self.assertEqual(sorted(changed), sorted(set(VERSIONED) - {"README.md"}))
            self.assertEqual(self.versions_in(root), {"12.2026090700.0"})

    def test_version_free_readme_is_preserved(self):
        import contextlib

        with contextlib.ExitStack() as stack:
            root = self.fixture(stack)
            readme = root / "README.md"
            before = readme.read_bytes()
            changed = self.bump.rewrite_all(root, self.STALE, "12.2026090700.0")
            self.assertNotIn("README.md", changed)
            self.assertEqual(readme.read_bytes(), before)

    def test_readme_cache_paths_are_rewritten_too(self):
        # check.py fails on ANY stale version in README, and the uninstall
        # commands embed it in plugin-cache paths -- the easiest one to miss.
        import contextlib

        with contextlib.ExitStack() as stack:
            root = self.fixture(stack)
            old = self.bump.current_version(root)
            self.assertEqual(old, self.STALE)
            (root / "README.md").write_text(
                f"Version {old}\nCache: plugins/leos-agent/{old}\n", encoding="utf-8"
            )
            with pinned(self.bump, 2026, 9, 7):
                self.bump.rewrite_all(root, old, "12.2026090700.0")
            readme = (root / "README.md").read_text(encoding="utf-8")
            self.assertNotIn(old, readme)
            self.assertIn("12.2026090700.0", readme)

    def test_dry_run_writes_nothing(self):
        import contextlib

        with contextlib.ExitStack() as stack:
            root = self.fixture(stack)
            old = self.bump.current_version(root)
            self.assertEqual(old, self.STALE)
            before = {rel: (root / rel).read_bytes() for rel in VERSIONED}
            with pinned(self.bump, 2026, 9, 7):
                self.bump.rewrite_all(root, old, "12.2026090700.0", dry_run=True)
            for rel in VERSIONED:
                self.assertEqual((root / rel).read_bytes(), before[rel], rel)


class TestCheckMode(BumpFixture):
    # do_check reports to stdout; swallow it so a passing suite stays readable.
    def check(self, root):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            return self.bump.do_check(root)

    def test_check_fails_on_a_stale_version(self):
        import contextlib

        with contextlib.ExitStack() as stack:
            root = self.fixture(stack)
            with pinned(self.bump, 2026, 9, 7):
                self.assertNotEqual(self.check(root), 0)

    def test_check_passes_once_bumped(self):
        import contextlib

        with contextlib.ExitStack() as stack:
            root = self.fixture(stack)
            old = self.bump.current_version(root)
            self.assertEqual(old, self.STALE)
            with pinned(self.bump, 2026, 9, 7):
                self.bump.rewrite_all(root, old, "12.2026090700.0")
                self.assertEqual(self.check(root), 0)


if __name__ == "__main__":
    unittest.main()
