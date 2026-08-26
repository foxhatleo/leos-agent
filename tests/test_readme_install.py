"""The README is the install surface. This checks it stays true, not verbatim.

The previous version pinned roughly fifty-five literal strings, so ordinary
copy-editing failed the build while a genuinely wrong install command would
have passed as long as its substring survived somewhere on the page. What is
checked here instead: every supported harness has a working install path, no
removed surface is still advertised, and the roster and models come from
config/models.json rather than from a second hand-maintained list.
"""

import json
import os
import re
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
MODELS = os.path.join(PAYLOAD, "config", "models.json")


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


def _config():
    with open(MODELS, encoding="utf-8") as fh:
        return json.load(fh)


def _skill_dirs():
    names = set()
    for root in ("skills", "skills-claude"):
        base = os.path.join(PAYLOAD, root)
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            if os.path.isfile(os.path.join(base, name, "SKILL.md")):
                names.add(name)
    return names


class TestReadmeInstall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = _read("README.md")
        cls.payload_readme = _read("plugins", "leo", "README.md")
        cls.security = _read("SECURITY.md")

    def test_every_harness_has_an_install_path(self):
        for command in (
            "claude plugin marketplace add foxhatleo/leos-agent",
            "claude plugin install leo@leos-agent",
            "codex plugin marketplace add foxhatleo/leos-agent",
            "codex plugin add leo@leos-agent",
            "/add-plugin leo",
            "hermes plugins install foxhatleo/leos-agent --enable",
            "opencode plugin leos-agent --global",
        ):
            with self.subTest(command=command):
                self.assertIn(command, self.readme)

    def test_every_configured_harness_is_documented(self):
        for harness in _config()["harnesses"].values():
            with self.subTest(harness=harness["title"]):
                self.assertIn(harness["title"], self.readme)

    def test_removed_surfaces_are_not_offered_as_components(self):
        """A removed skill may be named in the upgrade note — that is the point
        of an upgrade note — but must not be listed as something you can use.
        """
        start = self.readme.index("## What the plugin provides")
        section = self.readme[start : self.readme.index("## MCP integrations")]
        for gone in (
            "leo:setup", "leo:doctor", "leo:memory", "leo:writing-skills", "leo:using-leo",
        ):
            with self.subTest(token=gone):
                self.assertNotIn(gone, section)

    def test_removed_files_are_never_mentioned(self):
        for gone in ("session-start.py", "./install.sh", "CHANGELOG"):
            with self.subTest(token=gone):
                self.assertNotIn(gone, self.readme)

    def test_every_role_and_skill_is_named(self):
        """The README is the roster's second home; it must not drift from config."""
        for role in _config()["roles"]:
            with self.subTest(role=role):
                self.assertIn(role, self.readme)
        for skill in sorted(_skill_dirs()):
            with self.subTest(skill=skill):
                self.assertIn(skill, self.readme)

    def test_tier_table_matches_the_config(self):
        for harness, rows in _config()["harnesses"].items():
            for tier, row in rows["tiers"].items():
                with self.subTest(harness=harness, tier=tier):
                    self.assertIn(row["model"], self.readme)

    def test_no_hand_copied_release_version(self):
        """Versions live in the manifests; release.py is what checks them."""
        self.assertNotIn("Version `9.0.0`", self.readme)
        self.assertNotRegex(self.readme, r"\bv?9\.0\.0\b")

    def test_platform_support_is_stated(self):
        self.assertIn("macOS, Linux, and WSL", self.readme)
        self.assertIn("Native Windows is unsupported", self.readme)
        self.assertIn("Python 3.9", self.readme)

    def test_opencode_config_filenames_are_both_named(self):
        for name in ("opencode.json", "opencode.jsonc"):
            with self.subTest(filename=name):
                self.assertIn(name, self.readme)
        self.assertIn('"plugin": ["leos-agent"]', self.readme)

    def test_no_hand_copied_npm_cache_path(self):
        self.assertNotIn("~/.cache/opencode/node_modules", self.readme)

    def test_the_no_injection_contract_is_stated(self):
        """The headline property of 8.0, and the thing most worth not regressing."""
        self.assertIn("Nothing is injected", self.readme)
        self.assertIn("leo:routing", self.readme)


class TestPayloadReadme(unittest.TestCase):
    """The npm landing page, generated and OpenCode-scoped."""

    @classmethod
    def setUpClass(cls):
        cls.text = _read("plugins", "leo", "README.md")

    def test_is_generated(self):
        self.assertIn("Generated by scripts/render_adapters.py", self.text)

    def test_uses_the_opencode_skill_spelling(self):
        self.assertIn("leo-routing", self.text)
        self.assertNotIn("`leo:routing`", self.text)

    def test_names_the_opencode_install_contract(self):
        for token in (
            "opencode plugin leos-agent --global",
            "opencode auth login",
            "adapters/opencode/agents.json",
            "OpenRouter",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.text)


class TestContributing(unittest.TestCase):
    def test_lists_the_local_and_release_checks(self):
        text = _read("CONTRIBUTING.md")
        for command in (
            "python3 plugins/leo/scripts/render_adapters.py --check",
            "python3 -m unittest discover -s tests -v",
            "python3 tools/vendor/codex/validate_plugin.py plugins/leo",
            "node tools/vendor/cursor/validate-template.mjs",
            "python3 tools/release.py --check-version vX.Y.Z",
            "npm pack",
        ):
            with self.subTest(command=command):
                self.assertIn(command, text)

    def test_absorbs_the_skill_authoring_contract(self):
        """writing-skills left the payload; its content has to land somewhere."""
        text = _read("CONTRIBUTING.md")
        for token in ("when_to_use", "Authoring a skill", "listing text"):
            with self.subTest(token=token):
                self.assertIn(token, text)


class TestSecurity(unittest.TestCase):
    def test_reporting_stays_private(self):
        text = _read("SECURITY.md")
        self.assertIn("GitHub's private security advisory flow", text)
        self.assertIn("Do not open a public issue", text)
        self.assertNotIn("mailto:", text)


if __name__ == "__main__":
    unittest.main()
