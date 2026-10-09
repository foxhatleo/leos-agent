"""attach-pr must never pick a stranger's fork PR that shares a branch name.

`gh pr list --head <branch>` filters by branch name only, so a fork's PR with
the same name comes back alongside this repo's. The fake gh below does the
same, and returns only the --json fields asked for, so a resolver that stops
requesting the owner fields fails here rather than in a real session.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "resolve_attach_target.py"
sys.path.insert(0, str(ROOT / "scripts"))
import resolve_attach_target as resolver  # noqa: E402

FAKE_GH = r'''
import json, os, sys
state = json.load(open(os.environ["FAKE_GH_STATE"]))
args = sys.argv[1:]

def option(name):
    return args[args.index(name) + 1] if name in args else None

def project(pr):
    return {k: pr[k] for k in option("--json").split(",") if k in pr}

if args[:2] == ["auth", "status"]:
    sys.exit(0)
if args[:2] == ["repo", "view"]:
    print(state["repo"])
    sys.exit(0)
if args[:2] == ["pr", "list"]:
    head, search = option("--head"), option("--search")
    rows = [p for p in state["prs"] if (head is None or p["headRefName"] == head)
            and (search is None or search.upper() in (p["title"] + " " + p["headRefName"]).upper())]
    print(json.dumps([project(p) for p in rows]))
    sys.exit(0)
if args[:2] == ["pr", "view"]:
    for p in state["prs"]:
        if str(p["number"]) == args[2]:
            print(json.dumps(project(p)))
            sys.exit(0)
    print("Could not resolve to a PullRequest", file=sys.stderr)
    sys.exit(1)
sys.exit("fake gh: unhandled %r" % (args,))
'''


def pr(number, owner, cross, state="OPEN", head="feature-x", title="Some change"):
    return {"number": number, "url": "https://github.com/base/repo/pull/%d" % number, "headRefName": head,
            "baseRefName": "main", "state": state, "title": title, "isCrossRepository": cross,
            "headRepositoryOwner": {"id": "U_%s" % owner, "login": owner}}


class AttachTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "home").mkdir()
        (root / "gitconfig").write_text("")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!" + sys.executable + "\n" + FAKE_GH)
        gh.chmod(0o755)
        self.state = root / "gh-state.json"
        self.env = {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""), "HOME": str(root / "home"),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
                    "FAKE_GH_STATE": str(self.state), "PYTHONDONTWRITEBYTECODE": "1"}
        self.repo = root / "repo"
        origin = root / "origin.git"
        self.git("init", "-q", "--bare", str(origin), cwd=root)
        self.git("init", "-q", str(self.repo), cwd=root)
        self.git("commit", "-q", "--allow-empty", "-m", "init")
        self.git("branch", "feature-x")
        self.git("remote", "add", "origin", str(origin))

    def git(self, *args, cwd=None):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", "init.defaultBranch=main", *args], cwd=str(cwd or self.repo), env=self.env,
                       check=True, capture_output=True)

    def resolve(self, ident, repo, prs, push_url="https://github.com/leo/repo.git"):
        # Fetches stay local; only the push URL names a GitHub owner, as in a
        # checkout that pushes to the user's own fork.
        self.git("config", "remote.origin.pushurl", push_url)
        self.state.write_text(json.dumps({"repo": repo, "prs": prs}))
        result = subprocess.run([sys.executable, str(SCRIPT), ident], cwd=str(self.repo), env=self.env,
                                capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    def test_a_lone_open_fork_pr_with_the_same_branch_name_is_not_chosen(self):
        code, out = self.resolve("feature-x", "leo/repo", [pr(5, "stranger", True), pr(3, "leo", False, "CLOSED")])
        self.assertEqual((code, out["status"], out["pr_number"]), (0, "ok", 3))

    def test_only_fork_prs_is_an_error_that_names_them(self):
        code, out = self.resolve("feature-x", "leo/repo", [pr(5, "stranger", True)])
        self.assertEqual((code, out["status"]), (1, "error"))
        self.assertIn("#5 (stranger)", out["message"])

    def test_a_fork_checkout_accepts_prs_from_its_push_remote_owner(self):
        """Working from a fork: gh's repo is the upstream, the branch pushes to
        the user's fork, and their PR is cross-repository by design."""
        code, out = self.resolve("feature-x", "upstream/repo", [pr(8, "stranger", True), pr(7, "leo", True)],
                                 push_url="git@github.com:leo/repo.git")
        self.assertEqual((code, out["pr_number"], out["head_owner"]), (0, 7, "leo"))

    def test_ticket_search_sets_fork_prs_aside(self):
        prs = [pr(9, "stranger", True, head="other-DOCS-12", title="DOCS-12 fix"),
               pr(4, "leo", False, head="leo-docs-12", title="DOCS-12 fix")]
        code, out = self.resolve("DOCS-12", "leo/repo", prs)
        self.assertEqual((code, out["pr_number"]), (0, 4))

    def test_an_explicit_number_for_a_fork_pr_resolves_with_a_warning(self):
        code, out = self.resolve("5", "leo/repo", [pr(5, "stranger", True)])
        self.assertEqual((code, out["pr_number"]), (0, 5))
        self.assertIn("stranger", out["warning"])

    def test_own_pr_by_number_has_no_warning(self):
        code, out = self.resolve("#3", "leo/repo", [pr(3, "leo", False)])
        self.assertEqual(code, 0)
        self.assertNotIn("warning", out)


    def test_with_rtk_configured_the_attach_command_opts_out_of_its_rewrite(self):
        settings = Path(self.env["HOME"]) / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}]}}))
        self.git("checkout", "-q", "feature-x")
        code, out = self.resolve("feature-x", "leo/repo", [pr(3, "leo", False)])
        self.assertEqual((code, out["workdir_kind"], out["rtk_bypass"]), (0, "checkout", True))
        self.assertIsNone(rtk_rewrite(out["attach_command"]))


ENV_PREFIX = re.compile(r"""^(?:env\s+|[A-Z_][A-Z0-9_]*=(?:"[^"]*"|'[^']*'|[^\s;]+)\s+)+""")


def rtk_rewrite(command):
    """rtk's Claude hook, reduced to the rule that matters here: split on `;`,
    strip the env prefix, skip a segment whose prefix holds RTK_DISABLED=, and
    turn `gh pr ...` (without --json) into `rtk gh pr ...`, which runs the
    real gh. Returns the rewritten command, or None when nothing changes."""
    segments, changed = [], False
    for segment in command.split(";"):
        body = segment.strip()
        match = ENV_PREFIX.match(body)
        prefix, rest = (match.group(0), body[match.end():]) if match else ("", body)
        if "RTK_DISABLED=" not in prefix and re.match(r"gh\s+(pr|issue|run|repo|api|release)(\s|$)", rest) \
                and "--json" not in rest:
            body, changed = prefix + "rtk " + rest, True
        segments.append(body)
    return "; ".join(segments) if changed else None


class AttachCommand(unittest.TestCase):
    def test_rtk_would_turn_the_stub_into_a_real_pr_create_without_the_opt_out(self):
        plain = resolver.build_attach_command("/w", "https://github.com/o/r/pull/1", "main", "b")
        self.assertIn("rtk gh pr create --draft", rtk_rewrite(plain))
        guarded = resolver.build_attach_command("/w", "https://github.com/o/r/pull/1", "main", "b", rtk=True)
        self.assertIsNone(rtk_rewrite(guarded))

    def test_the_opt_out_still_runs_the_stub_and_prints_only_the_url(self):
        """bash keeps prefix assignments in scope for a function call, so the
        shadowed gh still echoes the URL; nothing named gh on PATH is run."""
        with tempfile.TemporaryDirectory() as tmp:
            command = resolver.build_attach_command(tmp, "https://github.com/o/r/pull/7", "main", "b", rtk=True)
            env = {"PATH": "/nonexistent", "HOME": tmp}
            result = subprocess.run(["/bin/bash", "-c", command], env=env, capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout), (0, "https://github.com/o/r/pull/7\n"))


class Ownership(unittest.TestCase):
    def test_remote_owner_forms(self):
        cases = {"git@github.com:leo/repo.git": "leo", "github-work:leo/repo.git": "leo",
                 "https://github.com/leo/repo.git": "leo", "https://github.com/leo/repo": "leo",
                 "ssh://git@github.com:22/leo/repo.git": "leo", "https://u:t@github.com/leo/repo/": "leo",
                 "/srv/git/repo.git": None, "file:///srv/leo/repo.git": None}
        for url, owner in cases.items():
            match = resolver.REMOTE_OWNER_RE.search(url)
            self.assertEqual(match.group(1) if match else None, owner, url)

    def test_a_pr_without_owner_fields_is_not_presumed_ours(self):
        self.assertFalse(resolver.is_own({"number": 1}, {"leo"}))
        self.assertFalse(resolver.is_own({"isCrossRepository": True, "headRepositoryOwner": None}, {"leo"}))
        self.assertTrue(resolver.is_own({"isCrossRepository": False}, {"leo"}))
        self.assertTrue(resolver.is_own({"isCrossRepository": True, "headRepositoryOwner": {"login": "Leo"}}, {"leo"}))

    def test_identifiers_with_a_trailing_newline_or_unicode_digits_are_not_numbers(self):
        self.assertIsNone(resolver.PR_NUM_RE.fullmatch("12\n"))
        self.assertIsNone(resolver.PR_NUM_RE.fullmatch("١٢"))


if __name__ == "__main__":
    unittest.main()
