"""Codex agent TOML lint: install/codex/agents/leo-*.toml. Stdlib unittest only
(tomllib, Python 3.11+).

Run: python3 -m unittest tests.test_codex_agents -v
"""

import os
import tomllib
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_DIR = os.path.join(REPO, "install", "codex", "agents")

# Claude agent stems this harness ports, minus expert — Codex has no Fable rung.
STEMS = ("explore", "investigator", "planner", "implementer", "executor", "reviewer")
EXPECTED_FILES = {f"leo-{stem}.toml" for stem in STEMS}

CODEX_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}

# Tier table: investigator/planner/reviewer = Opus (sol), implementer = Sonnet
# (terra), executor/explore = Haiku (luna).
EXPECTED_MODEL_BY_STEM = {
    "explore": "gpt-5.6-luna",
    "investigator": "gpt-5.6-sol",
    "planner": "gpt-5.6-sol",
    "implementer": "gpt-5.6-terra",
    "executor": "gpt-5.6-luna",
    "reviewer": "gpt-5.6-sol",
}

READ_ONLY_STEMS = {"investigator", "planner", "reviewer", "explore"}
ALLOWED_SANDBOX_MODES = {"read-only", "workspace-write"}


def _load(stem):
    path = os.path.join(AGENTS_DIR, f"leo-{stem}.toml")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


class TestAgentDirContents(unittest.TestCase):
    def test_exactly_the_six_no_expert(self):
        entries = {f for f in os.listdir(AGENTS_DIR) if f.endswith(".toml")}
        self.assertEqual(entries, EXPECTED_FILES)
        self.assertNotIn("leo-expert.toml", entries)


class TestEachAgentParses(unittest.TestCase):
    def test_name_and_description(self):
        for stem in STEMS:
            with self.subTest(stem=stem):
                data = _load(stem)
                self.assertEqual(data.get("name"), f"leo-{stem}")
                self.assertTrue(str(data.get("description", "")).strip())
                self.assertTrue(str(data.get("developer_instructions", "")).strip())

    def test_model_matches_tier_table(self):
        for stem in STEMS:
            with self.subTest(stem=stem):
                data = _load(stem)
                model = data.get("model")
                self.assertIn(model, CODEX_MODELS)
                self.assertEqual(model, EXPECTED_MODEL_BY_STEM[stem])

    def test_sandbox_mode(self):
        for stem in STEMS:
            with self.subTest(stem=stem):
                data = _load(stem)
                mode = data.get("sandbox_mode")
                self.assertIn(mode, ALLOWED_SANDBOX_MODES)
                if stem in READ_ONLY_STEMS:
                    self.assertEqual(mode, "read-only")


class TestNoLeakedTokens(unittest.TestCase):
    def test_no_1m_or_fable(self):
        for fname in sorted(EXPECTED_FILES):
            path = os.path.join(AGENTS_DIR, fname)
            with self.subTest(file=fname):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertNotIn("[1m]", text)
                self.assertNotIn("fable", text.lower())


if __name__ == "__main__":
    unittest.main()
