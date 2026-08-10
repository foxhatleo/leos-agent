"""Native substitution: what a harness already has, and what Leo therefore skips.

Parity in 8.0 means the same policy through each harness's own mechanism, so the
roster deliberately differs per harness. That is only safe if the difference is
declared and enforced — otherwise "Claude ships three agents" is indistinguishable
from "four agents failed to render".

Two things are checked: a `drop` really does remove the component from that
harness's output, and a `prefer` really does keep shipping it.
"""

import json
import os
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
MODELS = os.path.join(PAYLOAD, "config", "models.json")
REFERENCE = os.path.join(PAYLOAD, "skills", "routing", "references", "harnesses.md")

# Where packaging can actually honour an exclusion. Codex's vendored validator
# hard-codes plugin_root/"skills" and Cursor's requires a single directory, so
# neither can be handed a reduced skill set.
SKILL_DROP_HARNESSES = {"claude", "opencode"}


def _config():
    with open(MODELS, encoding="utf-8") as fh:
        return json.load(fh)


class TestNativeSubstitutions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = _config()
        with open(REFERENCE, encoding="utf-8") as fh:
            cls.reference = fh.read()

    def _natives(self, harness, kind):
        return self.config["harnesses"][harness].get("natives", {}).get(kind, {})

    def test_every_entry_names_a_real_component(self):
        roles = set(self.config["roles"])
        skills = set()
        for root in ("skills", "skills-claude"):
            base = os.path.join(PAYLOAD, root)
            if os.path.isdir(base):
                skills |= {
                    n for n in os.listdir(base)
                    if os.path.isfile(os.path.join(base, n, "SKILL.md"))
                }
        for harness in self.config["harnesses"]:
            for kind, known in (("roles", roles), ("skills", skills)):
                for name in self._natives(harness, kind):
                    with self.subTest(harness=harness, kind=kind, name=name):
                        self.assertIn(name, known)

    def test_every_entry_explains_itself(self):
        for harness in self.config["harnesses"]:
            for kind in ("roles", "skills"):
                for name, entry in self._natives(harness, kind).items():
                    with self.subTest(harness=harness, name=name):
                        self.assertIn(entry["verdict"], ("drop", "prefer"))
                        self.assertTrue(entry["native"].strip())
                        self.assertTrue(entry["reason"].strip())

    def test_dropped_roles_are_absent_from_that_harness_output(self):
        """The substitution has to be real, not just recorded."""
        checks = {
            "claude": os.path.join(PAYLOAD, "agents"),
            "cursor": os.path.join(PAYLOAD, "adapters", "cursor", "agents"),
        }
        for harness, directory in checks.items():
            shipped = {n[:-3] for n in os.listdir(directory) if n.endswith(".md")}
            natives = self._natives(harness, "roles")
            for name, entry in natives.items():
                with self.subTest(harness=harness, role=name):
                    if entry["verdict"] == "drop":
                        self.assertNotIn(name, shipped)
                    else:
                        self.assertIn(name, shipped)
            expected = set(self.config["roles"]) - {
                n for n, e in natives.items() if e["verdict"] == "drop"
            }
            self.assertEqual(shipped, expected, f"{harness} roster does not match its declarations")

    def test_dropped_roles_are_absent_from_opencode_agents(self):
        with open(os.path.join(PAYLOAD, "adapters", "opencode", "agents.json"), encoding="utf-8") as fh:
            agents = set(json.load(fh))
        natives = self._natives("opencode", "roles")
        expected = set(self.config["roles"]) - {
            n for n, e in natives.items() if e["verdict"] == "drop"
        }
        self.assertEqual(agents, expected)

    def test_preferred_skills_still_ship(self):
        """`prefer` means the skill is installed and the native comes first."""
        for harness in self.config["harnesses"]:
            for name, entry in self._natives(harness, "skills").items():
                if entry["verdict"] != "prefer":
                    continue
                with self.subTest(harness=harness, skill=name):
                    self.assertTrue(
                        os.path.isfile(os.path.join(PAYLOAD, "skills", name, "SKILL.md")),
                        f"{name} is 'prefer' on {harness} but does not ship",
                    )

    def test_no_drop_where_packaging_cannot_deliver_it(self):
        for harness in self.config["harnesses"]:
            for name, entry in self._natives(harness, "skills").items():
                if entry["verdict"] != "drop":
                    continue
                with self.subTest(harness=harness, skill=name):
                    self.assertIn(harness, SKILL_DROP_HARNESSES)
        for harness, excluded in self.config["skills"]["exclude"].items():
            with self.subTest(harness=harness):
                if excluded:
                    self.assertIn(harness, SKILL_DROP_HARNESSES)

    def test_every_substitution_is_disclosed_with_a_fallback(self):
        """A session must be able to see what is missing and what to do instead."""
        self.assertIn("Native substitutions here", self.reference)
        self.assertIn("fall back to Leo's", self.reference)
        for harness in self.config["harnesses"]:
            for kind in ("roles", "skills"):
                for name, entry in self._natives(harness, kind).items():
                    with self.subTest(harness=harness, name=name):
                        self.assertIn(entry["native"], self.reference)

    def test_a_harness_with_no_natives_says_so(self):
        for harness, rows in self.config["harnesses"].items():
            natives = rows.get("natives", {})
            if natives.get("roles") or natives.get("skills"):
                continue
            title = rows["title"]
            start = self.reference.index(f"## {title}")
            with self.subTest(harness=harness):
                self.assertIn("None. Every Leo role and skill is registered", self.reference[start:])


if __name__ == "__main__":
    unittest.main()
