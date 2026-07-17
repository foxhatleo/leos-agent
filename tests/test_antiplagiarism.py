"""Anti-plagiarism lint, two layers:

Layer A (denylist): our own banned phrases must never appear in any
skill or agent file, verbatim or otherwise.

Layer B (shingle overlap): our process skills must not share 8-word
shingles with a reference corpus (an external skill library we do not
read directly — see tests/fixtures/regen_shingles.py). Requires the
fixture tests/fixtures/superpowers_shingles.txt; skips cleanly if it is
absent.

Both layers also cover the per-harness mapping docs
(skills/using-leo/references/*.md) and the OpenCode plugin script
(.opencode/plugin/leo.js, scanned once that harness layer lands — skips
cleanly until then). The mapping docs are markdown like everything else
and use the same normalize() transform; leo.js is not markdown, so it is
scanned with strip_markdown=False ("strip nothing" — lowercase and
whitespace-collapse only, no code-fence/markdown-punctuation stripping).

normalize() and shingles() are imported by tests/fixtures/regen_shingles.py
so both sides of the comparison use an identical transform. Stdlib
unittest only.

Run: python3 -m unittest tests.test_antiplagiarism -v
"""

import hashlib
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(REPO, "skills")
AGENTS_DIR = os.path.join(REPO, "agents")
REFERENCES_DIR = os.path.join(SKILLS_DIR, "using-leo", "references")
OPENCODE_LEO_JS = os.path.join(REPO, ".opencode", "plugin", "leo.js")
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SHINGLES_FIXTURE = os.path.join(FIXTURES_DIR, "superpowers_shingles.txt")

DENYLIST = [
    "iron law",
    "you do not have a choice",
    "violating the letter",
    "violating the spirit",
    "your human partner",
    "no production code without",
    "no fixes without root cause",
    "no completion claims without",
    "1% chance",
    "you must use it",
    "sunk cost fallacy",
    "delete means delete",
    "fighting the harness",
    "question the architecture",
    "red flags",
]

# The nine process skills from using-leo's Skill index, plus the
# using-leo policy skill itself.
PROCESS_SKILLS = [
    "brainstorming",
    "debugging",
    "delegation",
    "executing-plans",
    "finishing-a-branch",
    "test-first",
    "verification",
    "worktrees",
    "writing-plans",
]
SHINGLE_SKILLS = PROCESS_SKILLS + ["using-leo"]

CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
MARKDOWN_PUNCT_RE = re.compile(r"[#*|>`_\-\[\]()]")
WHITESPACE_RE = re.compile(r"\s+")

SHINGLE_SIZE = 8


def normalize(text, strip_markdown=True):
    """Lowercase, collapse whitespace, and (by default) strip fenced code
    blocks and markdown punctuation first. Shared by this test module and
    tests/fixtures/regen_shingles.py so both sides of the shingle
    comparison are transformed identically.

    strip_markdown=False is "strip nothing" mode for non-markdown sources
    (e.g. leo.js): skip the code-fence/markdown-punctuation passes, since
    those would mangle ordinary JS syntax rather than clean it up."""
    if strip_markdown:
        text = CODE_FENCE_RE.sub(" ", text)
        text = MARKDOWN_PUNCT_RE.sub(" ", text)
    text = text.lower()
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text


def shingle_windows(text, strip_markdown=True):
    """Ordered list of consecutive SHINGLE_SIZE-word windows (as strings)
    after normalization."""
    words = [w for w in normalize(text, strip_markdown=strip_markdown).split(" ") if w]
    return [
        " ".join(words[i:i + SHINGLE_SIZE])
        for i in range(len(words) - SHINGLE_SIZE + 1)
    ]


def shingles(text, strip_markdown=True):
    """Set of sha1 hex digests, one per SHINGLE_SIZE-word window."""
    return {
        hashlib.sha1(window.encode("utf-8")).hexdigest()
        for window in shingle_windows(text, strip_markdown=strip_markdown)
    }


def _denylist_normalize(text):
    return WHITESPACE_RE.sub(" ", text.lower()).strip()


def _skill_and_agent_paths():
    paths = []
    for root, _dirs, files in os.walk(SKILLS_DIR):
        for f in files:
            if f == "SKILL.md":
                paths.append(os.path.join(root, f))
    for f in sorted(os.listdir(AGENTS_DIR)):
        if f.endswith(".md"):
            paths.append(os.path.join(AGENTS_DIR, f))
    return sorted(paths)


def _reference_md_paths():
    """Per-harness mapping docs under skills/using-leo/references/*.md
    (claude-mapping.md, codex-mapping.md, ...). Not SKILL.md files, so
    _skill_and_agent_paths() never sees them — scanned separately here."""
    if not os.path.isdir(REFERENCES_DIR):
        return []
    return sorted(
        os.path.join(REFERENCES_DIR, f)
        for f in os.listdir(REFERENCES_DIR)
        if f.endswith(".md")
    )


class TestNoDenylistedPhrases(unittest.TestCase):
    def test_skill_and_agent_files_clean(self):
        for path in _skill_and_agent_paths():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            normalized = _denylist_normalize(text)
            rel = os.path.relpath(path, REPO)
            for phrase in DENYLIST:
                with self.subTest(file=rel, phrase=phrase):
                    self.assertNotIn(phrase, normalized)


class TestNoDenylistedPhrasesExtraSources(unittest.TestCase):
    """Layer A denylist, extended to the per-harness mapping docs and (once
    it lands) the OpenCode plugin script. Both carry the same
    prose/comment plagiarism risk as a skill or agent file."""

    def test_reference_mapping_docs_clean(self):
        paths = _reference_md_paths()
        self.assertTrue(
            paths, f"expected at least one *.md file under {REFERENCES_DIR}"
        )
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            normalized = _denylist_normalize(text)
            rel = os.path.relpath(path, REPO)
            for phrase in DENYLIST:
                with self.subTest(file=rel, phrase=phrase):
                    self.assertNotIn(phrase, normalized)

    def test_opencode_leo_js_clean(self):
        if not os.path.isfile(OPENCODE_LEO_JS):
            self.skipTest(
                f"OpenCode layer not landed yet: missing {OPENCODE_LEO_JS}"
            )
        with open(OPENCODE_LEO_JS, encoding="utf-8") as fh:
            text = fh.read()
        # leo.js is not markdown, but the denylist phrases themselves have
        # no markdown punctuation in them, so the plain lowercase/whitespace
        # normalize used for denylist matching needs no strip_markdown knob.
        normalized = _denylist_normalize(text)
        rel = os.path.relpath(OPENCODE_LEO_JS, REPO)
        for phrase in DENYLIST:
            with self.subTest(file=rel, phrase=phrase):
                self.assertNotIn(phrase, normalized)


class TestNoSuperpowersShingleOverlap(unittest.TestCase):
    def test_process_skills_share_no_shingles(self):
        if not os.path.isfile(SHINGLES_FIXTURE):
            self.skipTest(
                "fixture missing: tests/fixtures/superpowers_shingles.txt "
                "— regenerate with python3 tests/fixtures/regen_shingles.py"
            )

        with open(SHINGLES_FIXTURE, encoding="utf-8") as fh:
            fixture_hashes = {line.strip() for line in fh if line.strip()}

        for skill in SHINGLE_SKILLS:
            path = os.path.join(SKILLS_DIR, skill, "SKILL.md")
            with self.subTest(skill=skill):
                self.assertTrue(os.path.isfile(path), f"missing {path}")
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()

                windows = shingle_windows(text)
                offending = [
                    (idx, window)
                    for idx, window in enumerate(windows)
                    if hashlib.sha1(window.encode("utf-8")).hexdigest() in fixture_hashes
                ]

                if offending:
                    idx, window = offending[0]
                    self.fail(
                        f"skills/{skill}/SKILL.md window #{idx} matches the "
                        f"superpowers shingle fixture ({len(offending)} "
                        f"offending window(s) total): {window!r}"
                    )


class TestNoSuperpowersShingleOverlapExtraSources(unittest.TestCase):
    """Layer B shingle overlap, extended to the per-harness mapping docs
    and (once it lands) the OpenCode plugin script. The mapping docs are
    markdown, hashed the same way as any SKILL.md; leo.js is scanned with
    strip_markdown=False ("strip nothing" — see module docstring), so the
    fixture must carry hashes generated the same way for a JS source to
    ever match (see tests/fixtures/regen_shingles.py)."""

    def test_reference_mapping_docs_share_no_shingles(self):
        if not os.path.isfile(SHINGLES_FIXTURE):
            self.skipTest(
                "fixture missing: tests/fixtures/superpowers_shingles.txt "
                "— regenerate with python3 tests/fixtures/regen_shingles.py"
            )

        with open(SHINGLES_FIXTURE, encoding="utf-8") as fh:
            fixture_hashes = {line.strip() for line in fh if line.strip()}

        for path in _reference_md_paths():
            rel = os.path.relpath(path, REPO)
            with self.subTest(file=rel):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()

                windows = shingle_windows(text)
                offending = [
                    (idx, window)
                    for idx, window in enumerate(windows)
                    if hashlib.sha1(window.encode("utf-8")).hexdigest() in fixture_hashes
                ]

                if offending:
                    idx, window = offending[0]
                    self.fail(
                        f"{rel} window #{idx} matches the superpowers "
                        f"shingle fixture ({len(offending)} offending "
                        f"window(s) total): {window!r}"
                    )

    def test_opencode_leo_js_shares_no_shingles(self):
        if not os.path.isfile(SHINGLES_FIXTURE):
            self.skipTest(
                "fixture missing: tests/fixtures/superpowers_shingles.txt "
                "— regenerate with python3 tests/fixtures/regen_shingles.py"
            )
        if not os.path.isfile(OPENCODE_LEO_JS):
            self.skipTest(
                f"OpenCode layer not landed yet: missing {OPENCODE_LEO_JS}"
            )

        with open(SHINGLES_FIXTURE, encoding="utf-8") as fh:
            fixture_hashes = {line.strip() for line in fh if line.strip()}

        with open(OPENCODE_LEO_JS, encoding="utf-8") as fh:
            text = fh.read()

        windows = shingle_windows(text, strip_markdown=False)
        offending = [
            (idx, window)
            for idx, window in enumerate(windows)
            if hashlib.sha1(window.encode("utf-8")).hexdigest() in fixture_hashes
        ]

        if offending:
            idx, window = offending[0]
            self.fail(
                f".opencode/plugin/leo.js window #{idx} matches the "
                f"superpowers shingle fixture ({len(offending)} offending "
                f"window(s) total): {window!r}"
            )


if __name__ == "__main__":
    unittest.main()
