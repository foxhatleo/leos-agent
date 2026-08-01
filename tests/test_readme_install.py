"""README is the setup surface for every supported harness."""

import os
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestReadmeInstall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
            cls.readme = fh.read()

    def test_native_install_commands(self):
        for command in (
            "claude plugin marketplace add foxhatleo/leos-agent",
            "claude plugin install leo@leos-agent",
            "codex plugin marketplace add foxhatleo/leos-agent",
            "codex plugin add leo@leos-agent",
            "/add-plugin leo",
            "/add-plugin leo@https://github.com/foxhatleo/leos-agent",
            "hermes plugins install foxhatleo/leos-agent --enable",
            "opencode plugin leos-agent --global",
        ):
            with self.subTest(command=command):
                self.assertIn(command, self.readme)

    def test_no_hand_copied_npm_cache_path(self):
        """The old skills.paths fallback named a directory that does not
        exist: npm installs under ~/.cache/opencode/packages/<pkg>@<tag>/,
        and the literal drifted with an upstream layout change without
        anything noticing. plugin.js resolves its own location from
        import.meta.url, so no such path belongs in the docs at all.
        """
        self.assertNotIn("~/.cache/opencode/node_modules", self.readme)
        self.assertNotIn('"skills": { "paths"', self.readme)

    def test_opencode_config_filenames_are_both_named(self):
        for name in ("opencode.json", "opencode.jsonc"):
            with self.subTest(name=name):
                self.assertIn(name, self.readme)

    def test_models_and_retired_surfaces(self):
        for value in (
            "GPT-5.6 Sol",
            "Grok 4.5",
            "Composer 2.5",
            "gpt-5.6-terra",
            "moonshotai/kimi-k3",
            "z-ai/glm-5.2",
            "/model moonshotai/kimi-k3 --provider openrouter",
            "/model z-ai/glm-5.2 --provider openrouter",
        ):
            self.assertIn(value, self.readme)
        self.assertNotIn("./install.sh", self.readme)
        self.assertIn('"plugin": ["leos-agent"]', self.readme)
        self.assertIn("opencode.json", self.readme)
        self.assertIn("MCP", self.readme)
        self.assertIn("independently", self.readme)


if __name__ == "__main__":
    unittest.main()
