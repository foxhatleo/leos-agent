"""OpenCode plugin packaging lint: adapters/opencode/agents.json,
adapters/opencode/plugin.js, and plugins/leo/package.json. Stdlib
unittest only — the plugin bridge is ESM/Node, so this file checks it
statically, in the style of tests/test_workflow_static.py.

Run: python3 -m unittest tests.test_opencode_plugin -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expected_version import EXPECTED_VERSION

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
MODEL_CONFIG = os.path.join(PAYLOAD, "config", "models.json")
AGENTS_JSON = os.path.join(PAYLOAD, "adapters", "opencode", "agents.json")
PLUGIN_JS = os.path.join(PAYLOAD, "adapters", "opencode", "plugin.js")
PACKAGE_JSON = os.path.join(PAYLOAD, "package.json")

READ_ONLY = {"expert", "explore", "investigator", "planner", "reviewer"}
DENIED_RM = {"rm -rf ~", "rm -rf ~/*", "rm -rf /", "rm -rf /*"}


def _load_config():
    with open(MODEL_CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def _load_agents():
    with open(AGENTS_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _read_plugin_js():
    with open(PLUGIN_JS, encoding="utf-8") as fh:
        return fh.read()


class TestOpenCodeAgentsJson(unittest.TestCase):
    def test_exactly_six_roles_no_expert(self):
        agents = _load_agents()
        self.assertEqual(len(agents), 6)
        self.assertNotIn("expert", agents)

    def test_every_model_openrouter_prefixed_and_matches_config(self):
        config = _load_config()
        opencode = config["harnesses"]["opencode"]
        agents = _load_agents()
        for role, agent in agents.items():
            with self.subTest(role=role):
                tier = config["roles"][role]
                self.assertTrue(agent["model"].startswith("openrouter/"))
                self.assertEqual(agent["model"], f"openrouter/{opencode[tier]['model']}")

    def test_read_only_roles_carry_edit_deny(self):
        agents = _load_agents()
        for role, agent in agents.items():
            with self.subTest(role=role):
                if role in READ_ONLY:
                    self.assertEqual(agent["permission"], {"edit": "deny"})
                else:
                    self.assertEqual(
                        set(agent["permission"].get("bash", {})), DENIED_RM
                    )
                    for value in agent["permission"]["bash"].values():
                        self.assertEqual(value, "deny")

    def test_every_prompt_non_empty(self):
        agents = _load_agents()
        for role, agent in agents.items():
            with self.subTest(role=role):
                self.assertTrue(agent["prompt"].strip())

    def test_mode_is_subagent(self):
        agents = _load_agents()
        for role, agent in agents.items():
            with self.subTest(role=role):
                self.assertEqual(agent["mode"], "subagent")


class TestOpenCodePluginJsStatic(unittest.TestCase):
    def test_declares_the_four_hooks(self):
        text = _read_plugin_js()
        for hook in ("async config(config)", "'experimental.chat.system.transform'", "'tool.execute.before'"):
            with self.subTest(hook=hook):
                self.assertIn(hook, text)

    def test_references_leo_policy_marker(self):
        text = _read_plugin_js()
        self.assertIn("<leo-policy>", text)
        self.assertIn("</leo-policy>", text)

    def test_no_frontmatter_parser_or_env_overrides(self):
        text = _read_plugin_js()
        self.assertNotIn("LEO_MODEL_", text)

    def test_references_bash_guard(self):
        text = _read_plugin_js()
        self.assertIn("bash-guard.py", text)

    def test_references_agents_json_not_agents_dir_parsing(self):
        text = _read_plugin_js()
        self.assertIn("agents.json", text)


class TestOpenCodePackageJson(unittest.TestCase):
    def test_name_and_version(self):
        with open(PACKAGE_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["name"], "leos-agent")
        self.assertEqual(data["version"], EXPECTED_VERSION)
        self.assertEqual(data["main"], "adapters/opencode/plugin.js")

    def test_files_exclude_claude_only_surfaces(self):
        with open(PACKAGE_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        for excluded in ("skills-claude/", "skills-claude"):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, data["files"])


if __name__ == "__main__":
    unittest.main()
