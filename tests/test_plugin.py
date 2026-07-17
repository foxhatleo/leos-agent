"""Plugin packaging lint: .claude-plugin/ manifests and .mcp.json.
Stdlib unittest only.

Run: python3 -m unittest tests.test_plugin -v
"""

import json
import os
import re
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO, ".claude-plugin")
PLUGIN_JSON = os.path.join(PLUGIN_DIR, "plugin.json")
MARKETPLACE_JSON = os.path.join(PLUGIN_DIR, "marketplace.json")
MCP_JSON = os.path.join(REPO, ".mcp.json")

KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class TestPluginJson(unittest.TestCase):
    def test_valid_and_fields(self):
        data = _load(PLUGIN_JSON)
        self.assertEqual(data.get("name"), "leo")
        self.assertEqual(data.get("version"), "3.0.0")
        self.assertTrue(data.get("description", "").strip())
        self.assertRegex(data["name"], KEBAB_CASE_RE, f"name {data['name']!r} is not kebab-case")


class TestMarketplaceJson(unittest.TestCase):
    def test_valid_and_fields(self):
        data = _load(MARKETPLACE_JSON)
        self.assertEqual(data.get("name"), "leos-agent")
        self.assertIn("owner", data)
        self.assertTrue(data["owner"])

        plugins = data.get("plugins", [])
        self.assertTrue(plugins, "expected at least one entry in plugins")
        entry = plugins[0]
        self.assertEqual(entry.get("name"), "leo")
        self.assertEqual(entry.get("source"), "./")


class TestPluginDirHasOnlyManifests(unittest.TestCase):
    def test_no_nested_component_dirs(self):
        entries = sorted(os.listdir(PLUGIN_DIR))
        self.assertEqual(entries, ["marketplace.json", "plugin.json"])
        for entry in entries:
            self.assertTrue(os.path.isfile(os.path.join(PLUGIN_DIR, entry)))


class TestMcpJson(unittest.TestCase):
    def test_valid_and_servers(self):
        data = _load(MCP_JSON)
        servers = data.get("mcpServers", {})
        for name in ("linear-server", "atlassian", "slack"):
            with self.subTest(server=name):
                self.assertIn(name, servers)

        slack = servers.get("slack", {})
        headers = slack.get("headers", {})
        self.assertEqual(headers.get("Authorization"), "Bearer ${SLACK_MCP_TOKEN}")


class TestClaudePluginValidate(unittest.TestCase):
    def test_claude_plugin_validate(self):
        claude_bin = shutil.which("claude")
        if not claude_bin:
            self.skipTest("claude CLI not found on PATH")

        result = subprocess.run(
            [claude_bin, "plugin", "validate", "."],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"claude plugin validate . failed:\nstdout={result.stdout}\nstderr={result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
