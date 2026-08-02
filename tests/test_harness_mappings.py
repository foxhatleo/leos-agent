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
# Claude-only skills live in their own root (see test_consistency) so the
# directory-valued Cursor and Codex manifests cannot ship them.
CLAUDE_SKILLS_DIR = os.path.join(REPO, "plugins", "leo", "skills-claude")
SKILL_ROOTS = (SKILLS_DIR, CLAUDE_SKILLS_DIR)
REFERENCES_DIR = os.path.join(SKILLS_DIR, "using-leo", "references")

HARNESSES = ("claude", "codex", "cursor", "hermes", "opencode")

REQUIRED_SUBSTRINGS = {
    "claude": ("opus", "sonnet", "fable", "haiku"),
    "codex": (
        "gpt-5.6-sol", "gpt-5.6-terra",
        "reasoning_effort", "generic subagent",
    ),
    "cursor": ("GPT-5.6 Sol", "Grok 4.5", "Composer 2.5", "model: inherit"),
    "hermes": ("openrouter", "moonshotai/kimi-k3", "z-ai/glm-5.2", "homogeneous"),
    "opencode": ("openrouter", "moonshotai/kimi-k3", "z-ai/glm-5.2", "permission.edit: deny"),
}

# Every mapping must state which visual-evidence rungs exist and where memory
# projects, or a session has to guess at both. Pinned per harness so a config
# entry going missing fails the build instead of rendering an empty paragraph.
CAPABILITY_SUBSTRINGS = ("Visual evidence here", "Memory projection here")
# The two harnesses whose only rung is a shell-driven driver must say so.
PLAYWRIGHT_HARNESSES = ("hermes", "opencode")

# Claude-only tokens that must never leak into the other harnesses.
LEAKED_TOKENS = ("opus[1m]", "sonnet[1m]", "CLAUDE_PLUGIN_ROOT")
# [1m] is /model syntax, not an agent-frontmatter model shape, so it must
# not appear in ANY mapping now — including Claude's.

NON_CLAUDE_HARNESSES = tuple(h for h in HARNESSES if h != "claude")

# One literal, shared by the mappings and by worktrees/SKILL.md, so renaming
# the convention cannot leave the two describing different directories.
WORKTREE_PATH = ".claude/worktrees/"


MODEL_CONFIG = os.path.join(REPO, "plugins", "leo", "config", "models.json")


def _config():
    import json

    with open(MODEL_CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def _path(harness):
    return os.path.join(REFERENCES_DIR, f"{harness}-mapping.md")


def _read(harness):
    with open(_path(harness), encoding="utf-8") as fh:
        return fh.read()


def _skill_dirs():
    found = set()
    for skill_root in SKILL_ROOTS:
        if not os.path.isdir(skill_root):
            continue
        found |= {
            d for d in os.listdir(skill_root)
            if os.path.isfile(os.path.join(skill_root, d, "SKILL.md"))
        }
    return found



class TestCapabilityNotes(unittest.TestCase):
    def test_every_mapping_states_visual_and_memory_capability(self):
        for harness in HARNESSES:
            text = _read(harness)
            for substring in CAPABILITY_SUBSTRINGS:
                with self.subTest(harness=harness, substring=substring):
                    self.assertIn(substring, text)

    def test_shell_only_harnesses_name_the_driver(self):
        for harness in PLAYWRIGHT_HARNESSES:
            with self.subTest(harness=harness):
                self.assertIn("Playwright", _read(harness))

    def test_repo_scoped_facts_are_documented_as_never_projected(self):
        """The constraint is easy to 'fix' by someone who does not know why it
        exists, so every mapping carries the reason, not just the rule."""
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                self.assertIn("Only global-scope", _read(harness))


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


class TestCapabilityMatrix(unittest.TestCase):
    """The generic guard. Per-harness prose is what let EnterWorktree, the
    workflow runner and SendMessage go undisclosed on three harnesses each:
    nothing forces a paragraph nobody wrote. A row answered by every harness
    cannot go missing from one of them without failing here.
    """

    def test_every_capability_row_is_answered_in_every_mapping(self):
        config = _config()
        for harness in HARNESSES:
            text = _read(harness)
            for row in config["capabilities"]:
                with self.subTest(harness=harness, row=row["key"]):
                    self.assertIn(f"| {row['label']} |", text)
                    self.assertIn(row["values"][harness]["note"], text)

    def test_worktree_tooling_is_disclosed_everywhere(self):
        """Was stated only in the OpenCode mapping, while worktrees/SKILL.md
        names the Claude tools and the path convention to all five."""
        self.assertIn("EnterWorktree", _read("claude"))
        for harness in NON_CLAUDE_HARNESSES:
            with self.subTest(harness=harness):
                text = _read(harness)
                self.assertNotIn("EnterWorktree", text)
                self.assertIn(WORKTREE_PATH, text)

    def test_the_worktree_path_convention_matches_the_skill(self):
        """One shared literal, so renaming the convention breaks loudly."""
        with open(os.path.join(SKILLS_DIR, "worktrees", "SKILL.md"), encoding="utf-8") as fh:
            self.assertIn(WORKTREE_PATH, fh.read())

    def test_workflow_runner_is_disclosed_everywhere(self):
        self.assertIn("cost-tiered-fix.js", _read("claude"))
        for harness in NON_CLAUDE_HARNESSES:
            with self.subTest(harness=harness):
                self.assertIn("Workflow runner", _read(harness))

    def test_follow_up_tool_is_disclosed_everywhere(self):
        """SendMessage sits in the portable delegation skill; before the
        matrix, no mapping said whether it existed on any harness."""
        self.assertIn("SendMessage", _read("claude"))
        self.assertIn("followup_task", _read("codex"))
        for harness in ("cursor", "hermes", "opencode"):
            with self.subTest(harness=harness):
                self.assertIn("none established", _read(harness))

    def test_opencode_discloses_its_absent_expert_role(self):
        text = _read("opencode")
        self.assertIn("`expert` is not registered as an agent", text)

    def test_opencode_workflow_note_matches_what_the_package_ships(self):
        """The tarball and the claim have to agree in whichever direction."""
        import json

        with open(os.path.join(REPO, "plugins", "leo", "package.json"), encoding="utf-8") as fh:
            files = json.load(fh)["files"]
        text = _read("opencode")
        if any(entry.rstrip("/") == "workflows" for entry in files):
            self.assertIn("nothing here executes it", text)
            self.assertNotIn("no `cost-tiered-fix.js` here", text)
        else:
            self.assertIn("no `cost-tiered-fix.js` here", text)


class TestClaudeExclusiveSkills(unittest.TestCase):
    """Claude's appendix used to have no skills section in either direction:
    it never named what only it has, and the exclusion list is rendered only
    for the others. A Claude session could not tell from its own mapping which
    skills do not travel."""

    def test_claude_mapping_enumerates_its_exclusive_skills(self):
        text = _read("claude")
        for name in _config()["skills"]["claudeOnly"]:
            with self.subTest(skill=name):
                self.assertIn(f"`leo:{name}`", text)

    def test_other_mappings_list_them_as_unavailable(self):
        for harness in NON_CLAUDE_HARNESSES:
            text = _read(harness)
            self.assertIn("## Leo skills not available here", text)
            for name in _config()["skills"]["claudeOnly"]:
                with self.subTest(harness=harness, skill=name):
                    self.assertIn(f"`leo:{name}`", text)


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
