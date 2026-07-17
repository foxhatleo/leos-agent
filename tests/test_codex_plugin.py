"""Codex plugin packaging lint: .codex-plugin/plugin.json. Stdlib unittest only.

Run: python3 -m unittest tests.test_codex_plugin -v
"""

import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_JSON = os.path.join(REPO, ".codex-plugin", "plugin.json")

ALLOWED_TOP_LEVEL_KEYS = {
    "id", "name", "version", "description", "skills", "apps", "mcpServers",
    "interface", "hooks", "author", "homepage", "repository", "license", "keywords",
}

REQUIRED_INTERFACE_KEYS = {
    "displayName", "shortDescription", "longDescription", "developerName",
    "category", "capabilities", "defaultPrompt",
}


def _load():
    with open(PLUGIN_JSON, encoding="utf-8") as fh:
        return json.load(fh)


class TestCodexPluginJson(unittest.TestCase):
    def test_valid_and_core_fields(self):
        data = _load()
        self.assertEqual(data.get("name"), "leo")
        self.assertEqual(data.get("version"), "3.1.0")
        self.assertEqual(data.get("skills"), "./skills/")
        self.assertEqual(data.get("hooks"), "./hooks/hooks.json")
        self.assertEqual(data.get("mcpServers"), "./.mcp.json")

    def test_top_level_keys_subset(self):
        data = _load()
        extra = set(data.keys()) - ALLOWED_TOP_LEVEL_KEYS
        self.assertFalse(extra, f"unexpected top-level keys: {extra}")

    def test_interface_fields_non_empty(self):
        data = _load()
        interface = data.get("interface", {})
        self.assertIsInstance(interface, dict)
        for key in REQUIRED_INTERFACE_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, interface)
                value = interface[key]
                if isinstance(value, str):
                    self.assertTrue(value.strip(), f"interface.{key} is empty")
                elif isinstance(value, list):
                    self.assertTrue(value, f"interface.{key} is empty")
                    for item in value:
                        self.assertTrue(str(item).strip())
                else:
                    self.fail(f"interface.{key} has unexpected type {type(value)!r}")


if __name__ == "__main__":
    unittest.main()
