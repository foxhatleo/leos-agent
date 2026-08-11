"""The always-on cost of installing this plugin.

8.0 exists because v7 put ~9,400 tokens into every session before any component
fired: a ~3,900-token injected policy block plus the listing text of every skill
and agent. The injected block is gone, but the listing text is not — it is the
delivery mechanism now, and it grows one helpful clause at a time.

So it gets a committed ceiling. This is the only test in the suite that guards a
product property rather than a correctness one, and it is the one most likely to
be "fixed" by raising the number. Raise it only with a reason worth the tokens.

The unit is bytes of listing text, not tokens: token counts need a tokenizer and
a network call, and the ratio is stable enough (~3.7 bytes/token for this prose)
that bytes are the honest proxy. `claude plugin details leo@leos-agent` reports
the real number.
"""

import os
import re
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")

# Ceilings, in bytes of always-on listing text.
#
# At the 8.0 rewrite: 8,835 bytes of skill listing text + 1,241 of agent
# descriptions = ~2,720 tokens, against ~9,400 for v7. The headroom below is
# deliberately small.
MAX_SKILL_LISTING_BYTES = 9_600
MAX_AGENT_DESCRIPTION_BYTES = 1_500

# Per-skill ceiling. The two core policy skills carry the routing burden for the
# whole plugin now that nothing is injected, so they are allowed more.
MAX_PER_SKILL_BYTES = 620
CORE_SKILLS = {"routing", "review-gate"}
MAX_PER_CORE_SKILL_BYTES = 900

# Claude Code truncates description + when_to_use at this length in the skill
# listing. A skill past it is silently cut mid-sentence.
HARNESS_TRUNCATION_LIMIT = 1_536


def _field(frontmatter, key):
    """Read one frontmatter field, block scalars included."""
    block = re.search(
        r"^" + key + r":\s*(?:>|\|)[-+]?\s*\n((?:[ \t]+.*\n?)*)", frontmatter, re.M
    )
    if block:
        return " ".join(l.strip() for l in block.group(1).splitlines() if l.strip())
    inline = re.search(r"^" + key + r":[ \t]*(.+)$", frontmatter, re.M)
    return inline.group(1).strip() if inline else ""


def _frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        match = re.match(r"---\n(.*?)\n---", fh.read(), re.S)
    return match.group(1) if match else ""


def _skill_paths():
    for root in ("skills", "skills-claude"):
        base = os.path.join(PAYLOAD, root)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name, "SKILL.md")
            if os.path.isfile(path):
                yield name, path


def _listing_bytes(path):
    fm = _frontmatter(path)
    return len(_field(fm, "description")) + len(_field(fm, "when_to_use"))


class TestAlwaysOnBudget(unittest.TestCase):
    def test_total_skill_listing_text(self):
        total = sum(_listing_bytes(path) for _name, path in _skill_paths())
        self.assertLessEqual(
            total,
            MAX_SKILL_LISTING_BYTES,
            f"skill listing text is {total} bytes (~{round(total / 3.7)} tokens), over the "
            f"{MAX_SKILL_LISTING_BYTES}-byte ceiling. Tighten a description rather than "
            "raising this.",
        )

    def test_total_agent_descriptions(self):
        agents = os.path.join(PAYLOAD, "agents")
        total = sum(
            len(_field(_frontmatter(os.path.join(agents, n)), "description"))
            for n in sorted(os.listdir(agents))
            if n.endswith(".md")
        )
        self.assertLessEqual(total, MAX_AGENT_DESCRIPTION_BYTES, f"agent descriptions total {total} bytes")

    def test_no_single_skill_dominates(self):
        for name, path in _skill_paths():
            ceiling = MAX_PER_CORE_SKILL_BYTES if name in CORE_SKILLS else MAX_PER_SKILL_BYTES
            with self.subTest(skill=name):
                self.assertLessEqual(_listing_bytes(path), ceiling, f"{name} listing text is too long")

    def test_nothing_is_silently_truncated_by_the_harness(self):
        for name, path in _skill_paths():
            with self.subTest(skill=name):
                self.assertLess(_listing_bytes(path), HARNESS_TRUNCATION_LIMIT)

    def test_every_skill_actually_has_listing_text(self):
        """A zero here would pass every ceiling above for the wrong reason."""
        for name, path in _skill_paths():
            with self.subTest(skill=name):
                self.assertGreater(_listing_bytes(path), 0)


if __name__ == "__main__":
    unittest.main()
