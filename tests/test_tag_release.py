"""Behavioral tests for tagging every commit on main.

The interesting cases here are the ones that only happen when two pushes race,
which is exactly what never shows up in a manual release. They are exercised
against real git repositories rather than mocked: a bare remote, two clones,
and a push that has to lose. What the assertions care about is what the remote
ends up holding, because a half-applied release -- a tag whose commit never
reached main, or a main that moved without its tag -- is the failure that
cannot be cleaned up automatically.
"""

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# Pinned rather than inherited, for the reason test_bump.py pins it: bumping
# from whatever the repo happens to be at makes these pass or fail depending on
# whether today's release has already been cut.
STALE = "9.9.9"

spec = importlib.util.spec_from_file_location("tag_release_test", ROOT / "scripts" / "tag-release.py")
tag_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tag_release)

# Taken from the script, not restated: the whole point of deriving it there is
# that one list of versioned files exists, and a copy here would defeat that.
VERSIONED = tag_release.VERSIONED


def git(*args, cwd, check=True):
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    if check and result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stdout}{result.stderr}")
    return result.stdout.strip()


class TestRejectionClassification(unittest.TestCase):
    """A rejection is only a race when another push actually won a ref.

    Every other rejection has to stay loud. Reporting a declined hook or a dead
    credential as "someone else got there first" turns a release pipeline that
    is broken into one that is silently doing nothing.
    """

    def test_a_branch_that_moved_is_a_race(self):
        for cause in ("non-fast-forward", "fetch first", "stale info"):
            with self.subTest(cause=cause):
                output = f" ! [rejected]        HEAD -> main ({cause})"
                self.assertEqual(tag_release.classify_rejection(output), "raced")

    def test_a_moved_branch_wins_over_the_tag_it_dragged_down(self):
        # Both refs are reported when an atomic push loses: the branch is behind
        # and the tag its version implies is therefore already taken. The branch
        # is the one that decides, because a newer tip means a newer run exists.
        output = (
            " ! [rejected]        v12.2026090900.0 -> v12.2026090900.0 (already exists)\n"
            " ! [rejected]        HEAD -> main (fetch first)"
        )
        self.assertEqual(tag_release.classify_rejection(output), "raced")

    def test_a_taken_tag_on_an_unmoved_branch_is_not_a_race(self):
        output = " ! [rejected]        v12.2026090900.0 -> v12.2026090900.0 (already exists)"
        self.assertEqual(tag_release.classify_rejection(output), "tag-taken")

    def test_a_declined_hook_is_never_a_race(self):
        output = " ! [remote rejected] HEAD -> main (protected branch hook declined)"
        self.assertEqual(tag_release.classify_rejection(output), "unknown")

    def test_a_dead_credential_is_never_a_race(self):
        self.assertEqual(tag_release.classify_rejection("fatal: Authentication failed"), "unknown")


class ReleaseRepo(unittest.TestCase):
    """A bare remote plus clones of it, carrying the real manifests."""

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        self.tmp = Path(tempfile.mkdtemp(prefix="leo tag release "))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--quiet", "--bare", str(self.origin)], check=True)
        # `git init --bare` points HEAD at whatever init.defaultBranch says, and a
        # clone of a repo whose HEAD names a branch that does not exist checks out
        # nothing at all.
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.origin)
        self.seed()

    def seed(self):
        seed = self.tmp / "seed"
        seed.mkdir()
        git("init", "--quiet", cwd=seed)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=seed)
        real = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        for rel in VERSIONED:
            dest = seed / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                (ROOT / rel).read_text(encoding="utf-8").replace(real, STALE), encoding="utf-8"
            )
        (seed / "scripts").mkdir(exist_ok=True)
        for script in ("bump.py", "tag-release.py"):
            shutil.copy2(ROOT / "scripts" / script, seed / "scripts" / script)
        git("add", "--all", cwd=seed)
        git("commit", "--quiet", "--message", "Seed", cwd=seed)
        git("remote", "add", "origin", str(self.origin), cwd=seed)
        git("push", "--quiet", "origin", "main", cwd=seed)

    def advance(self, clone, note="another push"):
        """Land an ordinary commit on the remote's main, ahead of a pending run."""
        (clone / "history.txt").write_text(f"{note}\n", encoding="utf-8")
        git("add", "--all", cwd=clone)
        git("commit", "--quiet", "--message", note, cwd=clone)
        git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=clone)

    def clone(self, name):
        path = self.tmp / name
        subprocess.run(
            ["git", "clone", "--quiet", str(self.origin), str(path)],
            check=True, capture_output=True,
        )
        return path

    def tag_release(self, clone, *extra):
        output = self.tmp / f"{clone.name}.output"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("GITHUB_OUTPUT", None)
        result = subprocess.run(
            [sys.executable, str(clone / "scripts" / "tag-release.py"),
             "--github-output", str(output), *extra],
            cwd=str(clone), capture_output=True, text=True, check=False, env=env,
        )
        outputs = {}
        if output.exists():
            for line in output.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                outputs[key] = value
        return result, outputs

    def remote_refs(self):
        listed = git("ls-remote", str(self.origin), cwd=self.tmp)
        refs = {}
        for line in listed.splitlines():
            sha, _, ref = line.partition("\t")
            refs[ref] = sha
        return refs


class TestAReleaseIsPreparedAtomically(ReleaseRepo):
    def test_a_clean_run_pushes_the_commit_and_its_tag_together(self):
        clone = self.clone("runner")
        result, outputs = self.tag_release(clone)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        version = json.loads((clone / "package.json").read_text(encoding="utf-8"))["version"]
        tag = f"v{version}"
        self.assertEqual(outputs, {"tag": tag, "released": "true"})

        refs = self.remote_refs()
        self.assertIn(f"refs/tags/{tag}", refs)
        # The annotated tag's peeled target, not the tag object, is what must sit
        # on main -- that is what the workflow's ancestor check resolves.
        self.assertEqual(refs[f"refs/tags/{tag}^{{}}"], refs["refs/heads/main"])
        self.assertEqual(
            git("log", "-1", "--format=%s", "origin/main", cwd=clone), f"Release {version}"
        )

    def test_the_release_commit_touches_only_the_versioned_files(self):
        clone = self.clone("runner")
        result, _ = self.tag_release(clone)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        changed = git("show", "--name-only", "--format=", "origin/main", cwd=clone).split()
        self.assertEqual(sorted(changed), sorted(set(VERSIONED) - {"README.md"}))

    def test_the_tag_is_annotated_like_every_release_tag_before_it(self):
        clone = self.clone("runner")
        self.tag_release(clone)
        version = json.loads((clone / "package.json").read_text(encoding="utf-8"))["version"]
        self.assertEqual(git("cat-file", "-t", f"v{version}", cwd=clone), "tag")


class TestALosingRunChangesNothing(ReleaseRepo):
    def test_a_run_whose_branch_moved_defers_instead_of_retrying(self):
        # The loser is cloned first, at the tip its run was triggered for; main
        # then moves under it, which is the only way the two runs build anything
        # different. Two runs that would produce byte-identical release commits
        # do not race at all -- the second push is a no-op on refs that already
        # hold exactly what it was about to write.
        loser = self.clone("loser")
        stale_tip = git("rev-parse", "HEAD", cwd=loser)
        winner = self.clone("winner")
        self.advance(winner)

        won, _ = self.tag_release(winner)
        self.assertEqual(won.returncode, 0, won.stdout + won.stderr)
        settled = self.remote_refs()

        lost, outputs = self.tag_release(loser)
        # Deferring is a success: nothing is wrong, and the push that won has a
        # run of its own carrying everything this one was about to release.
        self.assertEqual(lost.returncode, 0, lost.stdout + lost.stderr)
        self.assertIn("deferring", lost.stdout)
        self.assertEqual(outputs, {"tag": "", "released": "false"})
        self.assertEqual(self.remote_refs(), settled)
        # The local half of "changed nothing": no orphan release commit, no tag
        # left behind to collide with the version the next run picks.
        self.assertEqual(git("rev-parse", "HEAD", cwd=loser), stale_tip)
        self.assertEqual(git("tag", "--list", cwd=loser), "")
        self.assertEqual(git("status", "--porcelain", cwd=loser), "")

    def test_a_tag_already_taken_without_the_branch_moving_is_an_error(self):
        # Not a race: main is exactly where this run found it, so no other run
        # is coming, and no bump reachable from here can pick a free version.
        # Deferring would stall releases silently, so it has to fail loudly.
        clone = self.clone("runner")
        subprocess.run(
            [sys.executable, str(clone / "scripts" / "bump.py")],
            cwd=str(clone), check=True, capture_output=True,
        )
        version = json.loads((clone / "package.json").read_text(encoding="utf-8"))["version"]
        git("checkout", "--quiet", "--", ".", cwd=clone)
        git("tag", f"v{version}", "origin/main", cwd=clone)
        git("push", "--quiet", "origin", f"v{version}", cwd=clone)
        git("tag", "--delete", f"v{version}", cwd=clone)
        before = self.remote_refs()

        result, outputs = self.tag_release(clone)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("disagree", result.stderr)
        self.assertEqual(outputs, {})
        # The whole point of the atomic push: the branch did not move either.
        self.assertEqual(self.remote_refs(), before)

    def test_a_push_failure_that_is_not_a_rejection_fails(self):
        clone = self.clone("runner")
        result, outputs = self.tag_release(clone, "--remote", str(self.tmp / "missing.git"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not a race", result.stderr)
        self.assertEqual(outputs, {})


class TestNothingIsPushedUnverified(ReleaseRepo):
    def test_a_failed_verification_leaves_the_remote_untouched(self):
        clone = self.clone("runner")
        before = self.remote_refs()
        result, outputs = self.tag_release(clone, "--verify", "exit 1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("verification failed", result.stderr)
        self.assertEqual(outputs, {})
        self.assertEqual(self.remote_refs(), before)
        self.assertEqual(git("tag", "--list", cwd=clone), "")

    def test_verification_runs_against_the_bumped_tree(self):
        # The failure a bump can introduce is a version string in a file bump.py
        # does not rewrite, so a check that ran before the bump would miss it.
        clone = self.clone("runner")
        # Outside the checkout: a probe committed into it would change what is
        # released, and one left untracked would trip the dirty-tree guard.
        probe = self.tmp / "probe.py"
        probe.write_text(
            "import json, pathlib, sys\n"
            "version = json.loads(pathlib.Path('package.json').read_text())['version']\n"
            "sys.exit(0 if version != %r else 1)\n" % STALE,
            encoding="utf-8",
        )
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))}"
        result, _ = self.tag_release(clone, "--verify", command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_dry_run_pushes_nothing(self):
        clone = self.clone("runner")
        before = self.remote_refs()
        result, outputs = self.tag_release(clone, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("would release", result.stdout)
        self.assertEqual(outputs, {})
        self.assertEqual(self.remote_refs(), before)
        # The rehearsal bumped the tree to verify it; it must not leave it bumped.
        self.assertEqual(git("status", "--porcelain", cwd=clone), "")


class TestTheCheckoutIsTrustedOnlyWhenItShouldBe(ReleaseRepo):
    def test_a_dirty_checkout_is_refused(self):
        clone = self.clone("runner")
        before = self.remote_refs()
        (clone / "README.md").write_text("edited\n", encoding="utf-8")
        result, _ = self.tag_release(clone)
        self.assertEqual(result.returncode, 1)
        self.assertIn("dirty checkout", result.stderr)
        self.assertEqual(self.remote_refs(), before)

    def test_an_untracked_file_is_refused_too(self):
        clone = self.clone("runner")
        (clone / "leftover.txt").write_text("residue\n", encoding="utf-8")
        result, _ = self.tag_release(clone)
        self.assertEqual(result.returncode, 1)
        self.assertIn("leftover.txt", result.stderr)

    def test_a_head_that_is_not_the_triggering_commit_is_refused(self):
        clone = self.clone("runner")
        before = self.remote_refs()
        result, _ = self.tag_release(clone, "--expect-sha", "0" * 40)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not the", result.stderr)
        self.assertEqual(self.remote_refs(), before)

    def test_the_triggering_commit_is_accepted(self):
        clone = self.clone("runner")
        head = git("rev-parse", "HEAD", cwd=clone)
        result, _ = self.tag_release(clone, "--expect-sha", head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
