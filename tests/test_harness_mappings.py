"""Per-harness mapping appendix lint: skills/using-leo/references/*.md.
Content pins per harness, plus an anti-leak check that Claude-only tokens
never bleed into the other harnesses, and a cross-reference check that every
leo:<name> token resolves to a real skill dir. Stdlib unittest only.

Run: python3 -m unittest tests.test_harness_mappings -v
"""

import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(REPO, "plugins", "leo", "skills")
REFERENCES_DIR = os.path.join(SKILLS_DIR, "using-leo", "references")

HARNESSES = ("claude", "codex", "cursor", "hermes")

REQUIRED_SUBSTRINGS = {
    "claude": ("${user_config.opus_model}", "${user_config.sonnet_model}"),
    "codex": (
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "reasoning_effort", "generic subagent",
    ),
    "cursor": ("GPT-5.6 Sol", "Grok 4.5", "Composer 2.5", "model: inherit"),
    "hermes": ("openrouter", "moonshotai/kimi-k3", "z-ai/glm-5.2", "homogeneous"),
}

# Claude-only tokens that must never leak into the other harnesses.
LEAKED_TOKENS = ("user_config.opus_model", "user_config.sonnet_model", "CLAUDE_PLUGIN_ROOT")

NON_CLAUDE_HARNESSES = tuple(h for h in HARNESSES if h != "claude")


def _path(harness):
    return os.path.join(REFERENCES_DIR, f"{harness}-mapping.md")


def _read(harness):
    with open(_path(harness), encoding="utf-8") as fh:
        return fh.read()


def _skill_dirs():
    if not os.path.isdir(SKILLS_DIR):
        return set()
    return {
        d for d in os.listdir(SKILLS_DIR)
        if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md"))
    }


class TestMappingFilesExist(unittest.TestCase):
    def test_exists_and_non_empty(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                path = _path(harness)
                self.assertTrue(os.path.isfile(path), f"missing {path}")
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertTrue(text.strip(), f"{path} is empty")


class TestRequiredSubstrings(unittest.TestCase):
    def test_per_harness_pins(self):
        for harness, substrings in REQUIRED_SUBSTRINGS.items():
            text = _read(harness)
            for substring in substrings:
                with self.subTest(harness=harness, substring=substring):
                    self.assertIn(substring, text)


class TestAntiLeak(unittest.TestCase):
    def test_claude_only_tokens_never_leak(self):
        for harness in NON_CLAUDE_HARNESSES:
            text = _read(harness)
            for token in LEAKED_TOKENS:
                with self.subTest(harness=harness, token=token):
                    self.assertNotIn(token, text)


class TestCrossReferences(unittest.TestCase):
    def test_every_leo_token_resolves_to_a_skill_dir(self):
        dirs = _skill_dirs()
        for harness in HARNESSES:
            text = _read(harness)
            for tok in re.findall(r"leo:[a-z-]+", text):
                name = tok[len("leo:"):]
                with self.subTest(harness=harness, token=tok):
                    self.assertIn(name, dirs, f"{tok} in {harness}-mapping.md does not resolve to a skill dir")


if __name__ == "__main__":
    unittest.main()
