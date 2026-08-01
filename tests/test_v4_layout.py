"""Leo 4.0 marketplace and self-contained payload contracts."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expected_version import EXPECTED_VERSION


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugins", "leo")


def _load(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return json.load(fh)


class TestV4Layout(unittest.TestCase):
    def test_payload_contains_all_harness_manifests(self):
        for relative in (
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
            ".cursor-plugin/plugin.json",
            "config/models.json",
            "hooks/hooks.json",
            "skills/using-leo/SKILL.md",
        ):
            with self.subTest(path=relative):
                self.assertTrue(os.path.isfile(os.path.join(PLUGIN, relative)))

    def test_manifest_component_paths_stay_inside_payload(self):
        for harness in ("claude", "codex", "cursor"):
            data = _load("plugins", "leo", f".{harness}-plugin", "plugin.json")
            for field in ("agents", "skills", "hooks"):
                values = data.get(field, [])
                values = [values] if isinstance(values, str) else values
                for value in values:
                    if not isinstance(value, str):
                        continue
                    with self.subTest(harness=harness, field=field, value=value):
                        self.assertTrue(value.startswith("./"))
                        resolved = os.path.realpath(os.path.join(PLUGIN, value))
                        self.assertEqual(os.path.commonpath((PLUGIN, resolved)), PLUGIN)
                        self.assertTrue(os.path.exists(resolved))

    def test_payload_has_no_symlinks(self):
        for root, dirs, files in os.walk(PLUGIN):
            for name in dirs + files:
                path = os.path.join(root, name)
                with self.subTest(path=os.path.relpath(path, PLUGIN)):
                    self.assertFalse(os.path.islink(path))

    def test_marketplaces_resolve_to_nested_payload(self):
        claude = _load(".claude-plugin", "marketplace.json")
        self.assertEqual(claude["plugins"][0]["source"], "./plugins/leo")

        codex = _load(".agents", "plugins", "marketplace.json")
        entry = codex["plugins"][0]
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/leo"})
        self.assertEqual(
            entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertEqual(entry["category"], "Developer Tools")

        cursor = _load(".cursor-plugin", "marketplace.json")
        self.assertEqual(cursor["plugins"][0]["source"], "plugins/leo")

    def test_manifests_are_v4_and_do_not_bundle_mcp(self):
        for harness in ("claude", "codex", "cursor"):
            data = _load("plugins", "leo", f".{harness}-plugin", "plugin.json")
            with self.subTest(harness=harness):
                self.assertEqual(data["name"], "leo")
                self.assertEqual(data["version"], EXPECTED_VERSION)
                self.assertNotIn("mcpServers", data)
        codex = _load("plugins", "leo", ".codex-plugin", "plugin.json")
        self.assertNotIn("hooks", codex)

    def test_claude_components_use_conventional_paths(self):
        """Regression guard for the v4.0.0 load failures.

        A manifest "agents" array of file paths passes `claude plugin
        validate` but loads ZERO agents; the conventional agents/ directory
        auto-loads all seven. Declaring "hooks": "./hooks/hooks.json" makes
        the standard auto-loaded path load twice and fails the hook entirely.
        Both keys must stay absent.
        """
        manifest = _load("plugins", "leo", ".claude-plugin", "plugin.json")
        self.assertNotIn("agents", manifest)
        self.assertNotIn("hooks", manifest)

        # The manifest is entirely hand-maintained (the renderer no longer
        # writes any part of it), and Claude alone reads both skill roots.
        # Dropping ./skills-claude/ would silently stop shipping the three
        # operational skills with the rest of the suite still green.
        self.assertEqual(manifest["skills"], ["./skills/", "./skills-claude/"])

        agents_dir = os.path.join(PLUGIN, "agents")
        self.assertTrue(os.path.isdir(agents_dir), f"missing {agents_dir}")
        roles_dir = os.path.join(PLUGIN, "roles")
        self.assertEqual(
            sorted(n for n in os.listdir(agents_dir) if n.endswith(".md")),
            sorted(n for n in os.listdir(roles_dir) if n.endswith(".md")),
        )

    def test_removed_setup_surfaces_are_absent(self):
        # .opencode and package.json are no longer retired surfaces: the
        # OpenCode bridge and its npm manifest now live at the conventional
        # payload paths (plugins/leo/adapters/opencode/, plugins/leo/package.json).
        # This assertion is repo-root only, so it never sees them there.
        for relative in ("install.sh", "install", ".mcp.json"):
            with self.subTest(path=relative):
                self.assertFalse(os.path.lexists(os.path.join(REPO, relative)))

    def test_payload_package_json_excludes_claude_only_surfaces(self):
        data = _load("plugins", "leo", "package.json")
        self.assertEqual(data["name"], "leos-agent")
        for excluded in ("skills-claude/", ".claude-plugin/", ".codex-plugin/", ".cursor-plugin/"):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, data["files"])

    def test_operational_skills_treat_mcp_as_external(self):
        path = os.path.join(PLUGIN, "skills-claude", "resolve-ticket", "SKILL.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertRegex(text, r"does\s+not bundle MCP")
        self.assertNotIn("plugin's `.mcp.json`", text)


if __name__ == "__main__":
    unittest.main()
