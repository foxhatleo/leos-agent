"""Adversarial unit tests for /attach-pr's shell-facing resolver."""

import importlib.util
import os
import unittest
from unittest import mock


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "plugins", "leo", "scripts", "resolve_attach_target.py")
SPEC = importlib.util.spec_from_file_location("resolve_attach_target", SCRIPT)
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


class TestAttachInputValidation(unittest.TestCase):
    def test_rejects_shell_metacharacters_and_leading_dash_refs(self):
        for ref in ("feature space", "$(touch x)", "`touch x`", "feature;id", "-danger"):
            with self.subTest(ref=ref):
                self.assertFalse(resolver.is_safe_ref(ref))

    def test_validates_refs_with_git_check_ref_format(self):
        with mock.patch.object(resolver, "run", return_value=(0, "", "")) as run:
            self.assertTrue(resolver.is_safe_ref("feature/security-fix"))
        self.assertEqual(
            run.call_args.args[0],
            ["git", "check-ref-format", "--branch", "feature/security-fix"],
        )

    def test_pr_url_is_exact_public_github_pull_url(self):
        self.assertTrue(resolver.is_safe_pr_url("https://github.com/acme/widget/pull/42"))
        for url in (
            "http://github.com/acme/widget/pull/42",
            "https://evilgithub.com/acme/widget/pull/42",
            "https://github.com/acme/widget/pull/42?x=1",
            "https://github.com/acme/widget/pull/0",
        ):
            with self.subTest(url=url):
                self.assertFalse(resolver.is_safe_pr_url(url))

    def test_suggested_worktree_is_collision_safe(self):
        self.assertNotEqual(
            resolver.suggested_worktree("/repo", "feature/a-b"),
            resolver.suggested_worktree("/repo", "feature-a/b"),
        )

    def test_attach_command_quotes_every_shell_value(self):
        command = resolver.build_attach_command(
            "/tmp/work tree", "https://github.com/acme/widget/pull/42",
            "base/ref", "feature/space name",
        )
        self.assertIn("cd '/tmp/work tree'", command)
        self.assertIn("PR_URL=https://github.com/acme/widget/pull/42", command)
        self.assertIn("--base base/ref", command)
        self.assertIn("--head 'feature/space name'", command)
