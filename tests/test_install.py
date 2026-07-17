"""End-to-end tests for install.sh against a throwaway CLAUDE_DIR.

These exist because all three v3 install.sh defects were found live on a
real machine, not by review: the -e-follows-dangling-symlinks skip, the
'/leos-agent/' glob that never matched '.leos-agent', and the settings
merge whose shallow copy aliased local['hooks'] and skipped the write.
Every scenario here runs the real script, mode by mode.

Run: python3 -m unittest tests.test_install -v
"""

import json
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SH = os.path.join(REPO, "install.sh")


def run_install(mode, claude_dir, extra_env=None):
    env = dict(os.environ, CLAUDE_DIR=claude_dir)
    # Keep the script away from the real clone's state dir.
    env.setdefault("LEOS_AGENT_PATH", REPO)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", INSTALL_SH, mode],
        capture_output=True, text=True, env=env, timeout=60,
    )


class InstallTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude_dir = os.path.join(self.tmp.name, "claude")
        os.makedirs(self.claude_dir)

    def tearDown(self):
        self.tmp.cleanup()


class TestMigrateRemovesV2Symlinks(InstallTestCase):
    """migrate must remove dangling per-item v2 symlinks — the exact state a
    machine is in right after pulling the v3 restructure."""

    def setUp(self):
        super().setUp()
        # A fake deleted v2 layout: targets do NOT exist, links dangle —
        # and the clone dir is dotted, like the real ~/.leos-agent.
        self.fake_repo = os.path.join(self.tmp.name, ".leos-agent")
        for d in ("agents", "skills"):
            os.makedirs(os.path.join(self.claude_dir, d))
        os.symlink(
            os.path.join(self.fake_repo, "claude", "agents", "reviewer.md"),
            os.path.join(self.claude_dir, "agents", "reviewer.md"),
        )
        os.symlink(
            os.path.join(self.fake_repo, "claude", "skills", "review-pr"),
            os.path.join(self.claude_dir, "skills", "review-pr"),
        )
        # A foreign symlink and a real file must both survive.
        self.foreign_target = os.path.join(self.tmp.name, "elsewhere.md")
        open(self.foreign_target, "w").close()
        os.symlink(self.foreign_target,
                   os.path.join(self.claude_dir, "agents", "local-agent.md"))
        with open(os.path.join(self.claude_dir, "agents", "real.md"), "w") as fh:
            fh.write("machine-local agent\n")

    def links(self):
        found = []
        for root, _dirs, files in os.walk(self.claude_dir):
            for name in files + _dirs:
                p = os.path.join(root, name)
                if os.path.islink(p):
                    found.append(os.path.relpath(p, self.claude_dir))
        return sorted(found)

    def test_dangling_v2_links_removed_others_kept(self):
        result = run_install("migrate", self.claude_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        remaining = self.links()
        self.assertNotIn(os.path.join("agents", "reviewer.md"), remaining)
        self.assertNotIn(os.path.join("skills", "review-pr"), remaining)
        self.assertIn(os.path.join("agents", "local-agent.md"), remaining)
        self.assertTrue(
            os.path.isfile(os.path.join(self.claude_dir, "agents", "real.md")))

    def test_migrate_is_idempotent(self):
        run_install("migrate", self.claude_dir)
        second = run_install("migrate", self.claude_dir)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("fix", second.stdout)

    def test_check_flags_v2_links_without_touching(self):
        before = self.links()
        result = run_install("check", self.claude_dir)
        self.assertEqual(result.returncode, 1, "check must exit 1 on drift")
        self.assertEqual(self.links(), before, "check mode must not mutate")


class TestSettingsMerge(InstallTestCase):
    """settings must merge prefs, strip the stale bash-guard hook entry, and
    keep machine-local keys — in one write."""

    def setUp(self):
        super().setUp()
        self.settings_path = os.path.join(self.claude_dir, "settings.json")
        with open(self.settings_path, "w") as fh:
            # Faithful to a real v2 machine: every pref already merged, so
            # the ONLY delta settings-mode must produce is the hook strip.
            # (With any pref missing, the top-level merge masks the
            # shallow-copy aliasing this guards against.)
            json.dump({
                "permissions": {"defaultMode": "auto"},
                "tui": "fullscreen",
                "theme": "auto",
                "skipWorkflowUsageWarning": True,
                "agentPushNotifEnabled": True,
                "machineLocalKey": {"keep": True},
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{
                            "type": "command",
                            "command": 'python3 "$HOME/.claude/hooks/bash-guard.py"',
                            "timeout": 10,
                        }]},
                        {"matcher": "Write", "hooks": [{
                            "type": "command", "command": "echo local-hook",
                        }]},
                    ],
                },
            }, fh)

    def merged(self):
        with open(self.settings_path) as fh:
            return json.load(fh)

    def test_strips_stale_bash_guard_even_when_prefs_present(self):
        result = run_install("settings", self.claude_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = self.merged()
        pre = settings.get("hooks", {}).get("PreToolUse", [])
        commands = [h.get("command", "")
                    for e in pre for h in e.get("hooks", [])]
        self.assertFalse([c for c in commands if "bash-guard" in c],
                         f"stale bash-guard entry survived: {commands}")
        self.assertIn("echo local-hook", commands,
                      "foreign PreToolUse hook must survive")

    def test_prefs_merged_and_local_keys_survive(self):
        run_install("settings", self.claude_dir)
        settings = self.merged()
        self.assertEqual(settings["tui"], "fullscreen")
        self.assertEqual(settings["theme"], "auto")
        self.assertEqual(settings["machineLocalKey"], {"keep": True})

    def test_check_reports_stale_guard_as_drift(self):
        result = run_install("check", self.claude_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("bash-guard", result.stdout)


if __name__ == "__main__":
    unittest.main()
