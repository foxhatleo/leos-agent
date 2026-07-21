"""Canonical model matrix and generated adapter contracts."""

import json
import os
import subprocess
import sys
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugins", "leo")
MODELS = os.path.join(PLUGIN, "config", "models.json")
RENDERER = os.path.join(PLUGIN, "scripts", "render_adapters.py")


class TestModelMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(MODELS, encoding="utf-8") as fh:
            cls.data = json.load(fh)

    def test_exact_defaults(self):
        self.assertEqual(self.data["schemaVersion"], 1)
        self.assertEqual(
            self.data["roles"],
            {
                "expert": "fable",
                "planner": "opus",
                "investigator": "opus",
                "reviewer": "opus",
                "implementer": "sonnet",
                "executor": "haiku",
                "explore": "haiku",
            },
        )
        harnesses = self.data["harnesses"]
        self.assertEqual(
            harnesses["claude"],
            {
                "fable": {"model": "fable", "effort": "max"},
                "opus": {"model": "opus[1m]"},
                "sonnet": {"model": "sonnet[1m]"},
                "haiku": {"model": "haiku"},
            },
        )
        self.assertEqual(
            harnesses["cursor"],
            {
                "fable": {"model": "GPT-5.6 Sol"},
                "opus": {"model": "Grok 4.5"},
                "sonnet": {"model": "Grok 4.5"},
                "haiku": {"model": "Composer 2.5"},
            },
        )
        self.assertEqual(
            harnesses["codex"],
            {
                "fable": {"model": "gpt-5.6-sol", "effort": "max"},
                "opus": {"model": "gpt-5.6-sol", "effort": "high"},
                "sonnet": {"model": "gpt-5.6-terra", "effort": "medium"},
                "haiku": {"model": "gpt-5.6-luna", "effort": "low"},
            },
        )
        self.assertEqual(harnesses["hermes"]["provider"], "openrouter")
        self.assertEqual(harnesses["hermes"]["fable"]["model"], "moonshotai/kimi-k3")
        self.assertEqual(harnesses["hermes"]["opus"]["model"], "moonshotai/kimi-k3")
        self.assertEqual(harnesses["hermes"]["sonnet"]["model"], "z-ai/glm-5.2")
        self.assertEqual(harnesses["hermes"]["haiku"]["model"], "z-ai/glm-5.2")

    def test_renderer_reports_no_drift(self):
        result = subprocess.run(
            [sys.executable, RENDERER, "--check"],
            cwd=PLUGIN,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_renderer_is_idempotent(self):
        tracked = [
            RENDERER,
            os.path.join(PLUGIN, ".claude-plugin", "plugin.json"),
        ]
        for root in (
            os.path.join(PLUGIN, "adapters"),
            os.path.join(PLUGIN, "skills", "using-leo", "references"),
        ):
            for directory, _dirs, files in os.walk(root):
                tracked.extend(os.path.join(directory, name) for name in files)

        def snapshot():
            contents = {}
            for path in sorted(tracked):
                with open(path, "rb") as fh:
                    contents[path] = fh.read()
            return contents

        before = snapshot()
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, RENDERER],
                cwd=PLUGIN,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, snapshot())

    def test_generated_harness_agents(self):
        claude_dir = os.path.join(PLUGIN, "adapters", "claude", "agents")
        cursor_dir = os.path.join(PLUGIN, "adapters", "cursor", "agents")
        self.assertEqual(
            sorted(name for name in os.listdir(claude_dir) if name.endswith(".md")),
            [f"{name}.md" for name in sorted(self.data["roles"])],
        )
        for role, tier in self.data["roles"].items():
            with open(os.path.join(claude_dir, f"{role}.md"), encoding="utf-8") as fh:
                claude = fh.read()
            with open(os.path.join(cursor_dir, f"{role}.md"), encoding="utf-8") as fh:
                cursor = fh.read()
            self.assertTrue(claude.startswith("---\n"))
            self.assertTrue(cursor.startswith("---\n"))
            self.assertIn(f"model: ${{user_config.{tier}_model}}", claude)
            self.assertIn("model: inherit", cursor)

    def test_claude_user_config_defaults_match_matrix(self):
        manifest_path = os.path.join(PLUGIN, ".claude-plugin", "plugin.json")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        for tier, item in self.data["harnesses"]["claude"].items():
            with self.subTest(tier=tier):
                self.assertEqual(
                    manifest["userConfig"][f"{tier}_model"]["default"],
                    item["model"],
                )


if __name__ == "__main__":
    unittest.main()
