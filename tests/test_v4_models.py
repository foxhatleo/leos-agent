"""Canonical model matrix and generated adapter contracts."""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugins", "leo")
MODELS = os.path.join(PLUGIN, "config", "models.json")
RENDERER = os.path.join(PLUGIN, "scripts", "render_adapters.py")


def _renderer():
    spec = importlib.util.spec_from_file_location("leo_render_adapters", RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestModelMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(MODELS, encoding="utf-8") as fh:
            cls.data = json.load(fh)

    def test_exact_defaults(self):
        self.assertEqual(self.data["schemaVersion"], 4)
        self.assertEqual(
            self.data["roles"],
            {
                "expert": {"tier": "fable", "access": "read-only"},
                "planner": {"tier": "opus", "access": "read-only"},
                "investigator": {"tier": "opus", "access": "read-only"},
                "reviewer": {"tier": "opus", "access": "read-only"},
                "review-lens": {"tier": "sonnet", "access": "read-only"},
                "implementer": {"tier": "sonnet", "access": "write"},
                "executor": {"tier": "haiku", "access": "write"},
                "explore": {"tier": "haiku", "access": "read-only"},
            },
        )
        harnesses = self.data["harnesses"]
        self.assertEqual(
            harnesses["claude"],
            {
                "title": "Claude Code",
                "fable": {"model": "fable", "effort": "max"},
                "opus": {"model": "opus"},
                "sonnet": {"model": "sonnet"},
                "haiku": {"model": "haiku"},
            },
        )
        self.assertEqual(
            harnesses["cursor"],
            {
                "title": "Cursor",
                "fable": {"model": "GPT-5.6 Sol"},
                "opus": {"model": "Grok 4.5"},
                "sonnet": {"model": "Grok 4.5"},
                "haiku": {"model": "Composer 2.5"},
            },
        )
        self.assertEqual(
            harnesses["codex"],
            {
                "title": "Codex",
                "fable": {"model": "gpt-5.6-sol", "effort": "max"},
                "opus": {"model": "gpt-5.6-sol", "effort": "high"},
                "sonnet": {"model": "gpt-5.6-terra", "effort": "medium"},
                "haiku": {"model": "gpt-5.6-terra", "effort": "low"},
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
            os.path.join(PLUGIN, "agents"),
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
        # Claude agents live at the conventional agents/ path — a manifest
        # "agents" array of file paths validates but loads zero agents.
        claude_dir = os.path.join(PLUGIN, "agents")
        cursor_dir = os.path.join(PLUGIN, "adapters", "cursor", "agents")
        self.assertEqual(
            sorted(name for name in os.listdir(claude_dir) if name.endswith(".md")),
            [f"{name}.md" for name in sorted(self.data["roles"])],
        )
        for role, spec in self.data["roles"].items():
            tier = spec["tier"]
            with open(os.path.join(claude_dir, f"{role}.md"), encoding="utf-8") as fh:
                claude = fh.read()
            with open(os.path.join(cursor_dir, f"{role}.md"), encoding="utf-8") as fh:
                cursor = fh.read()
            self.assertTrue(claude.startswith("---\n"))
            self.assertTrue(cursor.startswith("---\n"))
            # Concrete alias, not a ${user_config.*} placeholder: Claude Code
            # never interpolates plugin userConfig into agent frontmatter.
            self.assertIn(f"model: {self.data['harnesses']['claude'][tier]['model']}\n", claude)
            self.assertIn("model: inherit", cursor)

    def test_validate_rejects_absenting_a_non_ceiling_tier(self):
        """Collapse alone does not justify absence.

        Sonnet and Haiku already share a model on OpenCode and Hermes, so a
        collapse-only rule would happily accept `absentTiers: ["sonnet"]` and
        silently delete `implementer` — a real rung with a real job. Only the
        ceiling tier's role exists solely to be stronger than the rung below.
        """
        renderer = _renderer()
        config = copy.deepcopy(self.data)
        opencode = config["harnesses"]["opencode"]
        self.assertEqual(opencode["sonnet"]["model"], opencode["haiku"]["model"],
                         "precondition: sonnet is genuinely collapsed here")
        opencode["absentTiers"] = ["sonnet"]
        with self.assertRaises(ValueError) as caught:
            renderer._validate(config)
        self.assertIn("only the ceiling", str(caught.exception))

    def test_validate_rejects_an_absent_tier_that_did_not_collapse(self):
        renderer = _renderer()
        config = copy.deepcopy(self.data)
        config["harnesses"]["opencode"]["fable"]["model"] = "some/distinct-model"
        with self.assertRaises(ValueError) as caught:
            renderer._validate(config)
        self.assertIn("has a model of its own", str(caught.exception))

    def test_collapse_uses_model_and_effort_identity(self):
        """Codex's Fable and Opus share a model name but are distinct rungs."""
        renderer = _renderer()
        self.assertEqual(renderer._collapse_note(self.data["harnesses"]["codex"]), "")

    def test_codex_live_agent_capabilities_are_recorded(self):
        rows = {row["key"]: row["values"]["codex"] for row in self.data["capabilities"]}
        self.assertEqual(rows["followUp"]["mode"], "tool")
        self.assertIn("followup_task", rows["followUp"]["note"])
        self.assertEqual(rows["askQuestion"]["mode"], "tool")
        self.assertIn("Plan mode", rows["askQuestion"]["note"])

    def test_validate_rejects_an_unanswered_capability(self):
        renderer = _renderer()
        config = copy.deepcopy(self.data)
        del config["capabilities"][0]["values"]["cursor"]
        with self.assertRaises(ValueError) as caught:
            renderer._validate(config)
        self.assertIn("unanswered", str(caught.exception))

    def test_validate_rejects_a_mode_outside_its_enum(self):
        renderer = _renderer()
        config = copy.deepcopy(self.data)
        config["capabilities"][0]["values"]["cursor"]["mode"] = "telepathy"
        with self.assertRaises(ValueError) as caught:
            renderer._validate(config)
        self.assertIn("not one of", str(caught.exception))

    def test_the_shipped_config_validates(self):
        _renderer()._validate(self.data)

    def test_claude_manifest_declares_no_model_user_config(self):
        manifest_path = os.path.join(PLUGIN, ".claude-plugin", "plugin.json")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertNotIn(
            "userConfig",
            manifest,
            "per-install model overrides were retired: retiering means editing "
            "config/models.json and re-rendering",
        )


if __name__ == "__main__":
    unittest.main()
