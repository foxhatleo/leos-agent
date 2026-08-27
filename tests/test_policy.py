"""Cost and routing properties the plugin promises to users."""

import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class TestRouting(unittest.TestCase):
    def test_codex_profiles_match_their_work(self):
        runner = (ROOT / "payload" / "codex-agents" / "leo-runner.toml").read_text(encoding="utf-8")
        executor = (ROOT / "payload" / "codex-agents" / "leo-executor.toml").read_text(encoding="utf-8")
        self.assertIn('model = "gpt-5.6-luna"', runner)
        self.assertIn('model_reasoning_effort = "low"', runner)
        self.assertIn('model = "gpt-5.6-terra"', executor)
        self.assertIn('model_reasoning_effort = "medium"', executor)

    def test_claude_agents_match_their_work(self):
        # The Claude twins bake the tier's model into the agent type, so a brief
        # that names the profile cannot silently inherit the parent model.
        runner = (ROOT / "agents" / "leo-runner.md").read_text(encoding="utf-8")
        executor = (ROOT / "agents" / "leo-executor.md").read_text(encoding="utf-8")
        self.assertIn("model: haiku", runner)
        self.assertIn("model: sonnet", executor)
        runner_tools = re.search(r"(?m)^tools:\s*(.+)$", runner).group(1)
        executor_tools = re.search(r"(?m)^tools:\s*(.+)$", executor).group(1)
        # The runner is the lens role for hostile-diff fan-outs: no Write, no Edit.
        self.assertNotIn("Edit", runner_tools)
        self.assertNotIn("Write", runner_tools)
        self.assertIn("Bash", runner_tools)
        self.assertIn("Edit", executor_tools)
        self.assertIn("Write", executor_tools)

    def test_clean_fork_flag_is_stated_everywhere_spawning_is_described(self):
        # A harness flag, not prose: every file that tells the model to spawn has
        # to name it, or one spawn path silently inherits the parent's history.
        # Assert the token only — the sentence around it is free to be reworded.
        sources = (
            ROOT / "rules" / "preferences.md",
            ROOT / "skills" / "review-pr" / "SKILL.md",
            ROOT / "skills" / "review-pr" / "reference" / "procedure.md",
        )
        for path in sources:
            with self.subTest(source=path.relative_to(ROOT).as_posix()):
                self.assertIn('fork_turns="none"', path.read_text(encoding="utf-8"))


class TestInvocationPolicy(unittest.TestCase):
    def test_explicit_only_portable_skills_have_codex_policy(self):
        for name in ("doctor", "handoff", "install"):
            path = ROOT / "skills" / name / "agents" / "openai.yaml"
            with self.subTest(skill=name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("interface:\n", text)
                self.assertIn("  display_name:", text)
                self.assertIn("  short_description:", text)
                self.assertIn("policy:\n  allow_implicit_invocation: false\n", text)

    def test_implicit_skills_are_not_accidentally_disabled(self):
        for name in ("handon", "review-pr"):
            path = ROOT / "skills" / name / "agents" / "openai.yaml"
            with self.subTest(skill=name):
                self.assertFalse(path.exists())


class TestStaticPromptBudget(unittest.TestCase):
    def test_committed_context_ceilings(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "measure_context.py"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_skill_descriptions_stay_single_line(self):
        for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
            text = path.read_text(encoding="utf-8")
            fm = text.split("---", 2)[1]
            match = re.search(r"(?m)^description:\s*(.+)$", fm)
            with self.subTest(skill=path.parent.name):
                self.assertIsNotNone(match)
                self.assertLessEqual(len(match.group(1).encode("utf-8")), 260)


if __name__ == "__main__":
    unittest.main()
