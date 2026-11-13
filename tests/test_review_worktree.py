"""review_worktree.py: a PR head checked out for reading, and approved runs in it.

Every test runs against a local bare "origin" that serves refs/pull/1/head,
reached through an insteadOf rewrite so the clone's origin still reads as a
GitHub URL. gh is replaced by a function, so nothing reaches GitHub.
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

_CONFIG_ENV = {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "HERMES_HOME", "PI_CODING_AGENT_DIR",
               "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "XDG_CONFIG_HOME", "LEOS_AGENT_LOCAL_PATH"}
# Inherited from a git hook, these would point fixture commands at the real checkout.
REPO_ENV = {"GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX", "GIT_GRAFT_FILE",
            "GIT_SHALLOW_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_NAMESPACE"}

_spec = importlib.util.spec_from_file_location("review_worktree", ROOT / "scripts" / "review_worktree.py")
rw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rw)


def git(*args, cwd):
    result = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                             "-c", "init.defaultBranch=main", *args],
                            cwd=str(cwd), capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


class FakeGh:
    def __init__(self, **view):
        self.view = dict({"state": "OPEN", "isCrossRepository": False}, **view)
        self.fail = False
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if self.fail:
            raise subprocess.CalledProcessError(1, ["gh"] + args, "", "HTTP 502")
        fields = args[args.index("--json") + 1].split(",")
        return json.dumps({k: self.view.get(k) for k in fields})


class WorktreeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.base = base
        env = {k: v for k, v in os.environ.items() if k not in _CONFIG_ENV | REPO_ENV}
        env.update(HOME=str(base / "home"), LEOS_AGENT_LOCAL_PATH=str(base / "local"),
                   GIT_CONFIG_NOSYSTEM="1", PATH="/usr/local/bin:/usr/bin:/bin")
        (base / "home").mkdir()
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

        work = base / "work"
        work.mkdir()
        git("init", "-q", cwd=work)
        (work / "app.py").write_text("print('base')\n")
        git("add", ".", cwd=work)
        git("commit", "-qm", "base", cwd=work)
        (work / "app.py").write_text("print('pr')\n")
        os.symlink("app.py", work / "link")
        git("add", ".", cwd=work)
        git("commit", "-qm", "pr", cwd=work)
        self.sha = git("rev-parse", "HEAD", cwd=work)
        self.base_sha = git("rev-parse", "HEAD~1", cwd=work)
        bare = base / "origin.git"
        git("init", "-q", "--bare", str(bare), cwd=base)
        git("push", "-q", str(bare), f"{self.base_sha}:refs/heads/main", f"{self.sha}:refs/pull/1/head", cwd=work)

        self.clone = base / "clone"
        git("clone", "-q", str(bare), str(self.clone), cwd=base)
        git("remote", "set-url", "origin", "https://github.com/o/r.git", cwd=self.clone)
        git("config", f"url.{bare}.insteadOf", "https://github.com/o/r.git", cwd=self.clone)

        self.gh = FakeGh(headRefOid=self.sha)
        gh_patch = mock.patch.object(rw, "gh", self.gh)
        gh_patch.start()
        self.addCleanup(gh_patch.stop)
        self.addCleanup(lambda: rw.remove_worktrees("o/r"))

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rw.main(list(argv))
        self.stderr = err.getvalue()
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None)

    def add(self, sha=None, repo="o/r"):
        return self.cli("add", "-R", repo, "-n", "1", "-C", str(self.clone), "--commit", sha or self.sha)

    def run_cmd(self, *cmd, approved="yes, run the tests", extra=()):
        return self.cli("run", "-R", "o/r", "-n", "1", "-C", str(self.clone), "--commit", self.sha,
                        "--approved", approved, *extra, "--", *cmd)

    def state(self):
        path = self.base / "local" / "review-worktrees.json"
        return json.loads(path.read_text()) if path.exists() else {}


class AddTests(WorktreeCase):
    def test_add_checks_out_the_pinned_head_detached(self):
        code, out = self.add()
        self.assertEqual(code, 0, self.stderr)
        path = Path(out["path"])
        self.assertEqual(path.parent, self.base / "local" / "review-worktrees" / "o__r")
        self.assertEqual(path.name, f"1-{self.sha[:12]}")
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), self.sha)
        self.assertEqual((path / "app.py").read_text(), "print('pr')\n")
        self.assertFalse(out["reused"])
        self.assertIs(out["cross_repository"], False)
        self.assertEqual(out["symlinks"], ["link"])
        self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(path.parent.parent).st_mode & 0o777, 0o700)
        entry = self.state()["o/r"]["1"]
        self.assertEqual((entry["path"], entry["commit"], entry["pgids"], entry["executed"]),
                         (str(path), self.sha, [], []))
        code, listed = self.cli("list", "-R", "o/r")
        self.assertEqual([(w["repo"], w["pr"], w["path"]) for w in listed["worktrees"]], [("o/r", 1, str(path))])

    def test_re_add_reuses_the_worktree(self):
        _, first = self.add()
        created = self.state()["o/r"]["1"]["created"]
        code, again = self.add()
        self.assertEqual(code, 0, self.stderr)
        self.assertTrue(again["reused"])
        self.assertEqual(again["path"], first["path"])
        self.assertEqual(self.state()["o/r"]["1"]["created"], created)

    def test_head_moved_is_refused(self):
        code, out = self.add(sha=self.base_sha)
        self.assertEqual(code, 4)
        self.assertIsNone(out)
        self.assertFalse((self.base / "local" / "review-worktrees" / "o__r" / f"1-{self.base_sha[:12]}").exists())
        self.assertNotIn("o/r", self.state())

    def test_origin_of_another_repo_is_refused(self):
        code, _ = self.add(repo="o/other")
        self.assertEqual(code, 3)
        self.assertEqual(self.state(), {})

    def test_fork_status_unknown_reports_null(self):
        self.gh.fail = True
        code, out = self.add()
        self.assertEqual(code, 0, self.stderr)
        self.assertIsNone(out["cross_repository"])

    def test_origin_url_forms(self):
        for url in ("https://github.com/o/r.git", "https://github.com/o/r", "git@github.com:o/r.git",
                    "ssh://git@github.com/o/r.git", "https://user@ghe.example.com/o/r/"):
            self.assertEqual(rw.origin_repo(url), "o/r", url)
        for url in ("/srv/git/o/r.git", "https://github.com/o/r/extra", ""):
            self.assertIsNone(rw.origin_repo(url), url)


class RemoveTests(WorktreeCase):
    def test_remove_refuses_a_path_outside_the_root(self):
        victim = self.base / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("x")
        path = self.base / "local" / "review-worktrees.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"o/r": {"1": {"path": str(victim), "commit": self.sha, "pgids": []}},
                                    "o/s": {"2": {"path": str(self.base / "local" / "review-worktrees" / "o__s"
                                                             / ".." / ".." / ".." / "victim"),
                                                  "commit": self.sha}}}))
        for repo, n in (("o/r", "1"), ("o/s", "2")):
            code, out = self.cli("remove", "-R", repo, "-n", n)
            self.assertEqual(code, 3)
            self.assertEqual(out["removed"], [])
            self.assertEqual(len(out["refused"]), 1)
        self.assertTrue((victim / "keep.txt").exists())
        self.assertEqual(self.state(), {})

    def test_remove_when_nothing_exists(self):
        self.assertEqual(rw.remove_worktrees("o/r", 1), {"removed": [], "killed": [], "refused": []})
        self.assertEqual(rw.remove_worktrees("o/r"), {"removed": [], "killed": [], "refused": []})
        self.assertEqual(rw.remove_worktrees("not a repo", 1)["removed"], [])
        self.assertFalse((self.base / "local").exists())
        code, out = self.cli("remove", "-R", "o/r", "-n", "1", "--all-heads")
        self.assertEqual((code, out["removed"]), (0, []))

    def test_remove_drops_the_worktree_and_state(self):
        _, added = self.add()
        report = rw.remove_worktrees("o/r", 1)
        self.assertEqual(report["removed"], [added["path"]])
        self.assertFalse(os.path.exists(added["path"]))
        self.assertNotIn("o/r", self.state())
        self.assertNotIn(added["path"], git("worktree", "list", cwd=self.clone))

    def test_all_heads_sweeps_an_unrecorded_head(self):
        _, added = self.add()
        self.state_path = self.base / "local" / "review-worktrees.json"
        self.state_path.write_text("{}")
        code, out = self.cli("remove", "-R", "o/r", "-n", "1")
        self.assertEqual(out["removed"], [])
        self.assertTrue(os.path.exists(added["path"]))
        code, out = self.cli("remove", "-R", "o/r", "-n", "1", "--all-heads")
        self.assertEqual(out["removed"], [added["path"]])
        self.assertFalse(os.path.exists(added["path"]))


class RunTests(WorktreeCase):
    def setUp(self):
        super().setUp()
        code, self.added = self.add()
        self.assertEqual(code, 0, self.stderr)

    def test_fork_is_refused(self):
        self.gh.view["isCrossRepository"] = True
        code, out = self.run_cmd("true")
        self.assertEqual((code, out), (6, None))
        self.gh.view["isCrossRepository"] = False
        self.gh.fail = True
        code, out = self.run_cmd("true")
        self.assertEqual((code, out), (6, None))
        self.assertEqual(self.state()["o/r"]["1"]["executed"], [])

    def test_closed_pr_and_moved_head_are_refused(self):
        self.gh.view["state"] = "MERGED"
        self.assertEqual(self.run_cmd("true")[0], 7)
        self.gh.view.update(state="OPEN", headRefOid=self.base_sha)
        self.assertEqual(self.run_cmd("true")[0], 4)

    def test_placeholder_approval_is_refused(self):
        for text in ("", "   ", "<the user's words>", "TEXT", "TODO"):
            self.assertEqual(self.run_cmd("true", approved=text)[0], 2, text)
        self.assertEqual(self.run_cmd("true", approved="yes")[0], 0, self.stderr)

    def test_missing_worktree_is_refused(self):
        rw.remove_worktrees("o/r", 1)
        self.assertEqual(self.run_cmd("true")[0], 3)

    def test_env_is_rebuilt_without_credentials(self):
        os.environ.update(GITHUB_TOKEN="ght", GH_TOKEN="ght2", SSH_AUTH_SOCK="/tmp/agent",
                          AWS_SECRET_ACCESS_KEY="aws", ANTHROPIC_API_KEY="k")
        code, out = self.run_cmd("env")
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(out["exit"], 0)
        env = dict(line.split("=", 1) for line in out["tail"].splitlines() if "=" in line)
        for name in ("GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY",
                     "LEOS_AGENT_LOCAL_PATH"):
            self.assertNotIn(name, env)
        self.assertEqual((env["CI"], env["HOST"], env["BIND"], env["NPM_CONFIG_USERCONFIG"]),
                         ("1", "127.0.0.1", "127.0.0.1", "/dev/null"))
        self.assertNotEqual(env["HOME"], os.environ["HOME"])
        self.assertTrue(env["TMPDIR"].startswith(env["HOME"]))
        self.assertNotIn("PORT", env)
        self.assertTrue(os.path.isfile(out["log"]))
        executed = self.state()["o/r"]["1"]["executed"]
        self.assertEqual([(e["cmd"], e["head"], e["exit"], e["approved"]) for e in executed],
                         [(["env"], self.sha, 0, "yes, run the tests")])

    def test_runs_in_the_worktree_and_reports_exit_and_tail(self):
        code, out = self.run_cmd("sh", "-c", "pwd; printf 'bell\\007\\n'; exit 3")
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(out["exit"], 3)
        lines = out["tail"].splitlines()
        self.assertEqual(os.path.realpath(lines[0]), os.path.realpath(self.added["path"]))
        self.assertEqual(lines[1], "bell\\x07")

    def test_timeout_kills_the_command(self):
        code, out = self.run_cmd("sleep", "30", extra=("--timeout", "1"))
        self.assertEqual(code, 0, self.stderr)
        self.assertTrue(out["timed_out"])
        self.assertIsNone(out["exit"])

    def test_served_process_group_is_killed_by_remove(self):
        code, out = self.run_cmd("sh", "-c", "sleep 60 & echo $PORT; wait", extra=("--serve", "--port", "18123"))
        self.assertEqual(code, 0, self.stderr)
        pgid = out["pgid"]
        self.assertEqual(out["port"], 18123)
        os.killpg(pgid, 0)  # running
        self.assertEqual(self.state()["o/r"]["1"]["pgids"], [pgid])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "18123" not in Path(out["log"]).read_text():
            time.sleep(0.05)
        self.assertIn("18123", Path(out["log"]).read_text())
        report = rw.remove_worktrees("o/r", 1)
        self.assertEqual(report["killed"], [pgid])
        self.assertFalse(rw._group_alive(pgid))
        self.assertFalse(os.path.exists(self.added["path"]))


if __name__ == "__main__":
    unittest.main()
