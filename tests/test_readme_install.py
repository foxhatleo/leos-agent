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
        ):
            with self.subTest(command=command):
                self.assertIn(command, self.readme)

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
        self.assertNotIn("OpenCode", self.readme)
        self.assertIn("MCP", self.readme)
        self.assertIn("independently", self.readme)


if __name__ == "__main__":
    unittest.main()
