"""The canonical matrix and the adapters generated from it.

Structural invariants, not a literal copy of models.json. Pinning the config
verbatim (which this file used to do) meant every retier failed a test that
was only restating the config back to itself, and told you nothing about
whether the config was coherent. The renderer's own `_validate` is the thing
worth testing, so each of its rules gets a negative test built by mutating a
deepcopy of the shipped config.
"""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugins", "leo")
MODELS = os.path.join(PLUGIN, "config", "models.json")
RENDERER = os.path.join(PLUGIN, "scripts", "render_adapters.py")

HARNESSES = ("claude", "codex", "cursor", "opencode")


def _renderer():
    spec = importlib.util.spec_from_file_location("leo_render_adapters", RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config():
    with open(MODELS, encoding="utf-8") as fh:
        return json.load(fh)


class TestMatrixShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _config()

    def test_schema_version(self):
        self.assertEqual(self.data["schemaVersion"], 5)

    def test_exactly_the_four_supported_harnesses(self):
        self.assertEqual(sorted(self.data["harnesses"]), sorted(HARNESSES))

    def test_no_trace_of_the_dropped_harness(self):
        with open(MODELS, encoding="utf-8") as fh:
            self.assertNotIn("hermes", fh.read().lower())

    def test_three_tiers_and_no_ceiling_rung(self):
        self.assertEqual(self.data["tiers"], ["opus", "sonnet", "haiku"])
        self.assertNotIn("fable", self.data["tiers"])

    def test_every_harness_answers_every_tier_with_a_model(self):
        for harness in HARNESSES:
            tiers = self.data["harnesses"][harness]["tiers"]
            with self.subTest(harness=harness):
                self.assertEqual(sorted(tiers), sorted(self.data["tiers"]))
                for tier, row in tiers.items():
                    self.assertTrue(row.get("model"), f"{harness}/{tier} has no model")
                    # The [1m] outage class: a context suffix reaches the
                    # model selector verbatim and kills the spawn.
                    self.assertNotIn("[", row["model"], f"{harness}/{tier} model has a suffix")

    def test_effort_is_all_or_nothing_per_harness(self):
        efforts = set(self.data["efforts"])
        for harness in HARNESSES:
            rows = self.data["harnesses"][harness]
            with self.subTest(harness=harness):
                if rows["effortSupport"]:
                    for tier, row in rows["tiers"].items():
                        self.assertIn(row.get("effort"), efforts, f"{harness}/{tier}")
                else:
                    for tier, row in rows["tiers"].items():
                        self.assertIsNone(row.get("effort"), f"{harness}/{tier}")
                    self.assertTrue(rows.get("effortAbsentNote"))

    def test_roles_are_well_formed(self):
        for role, spec in self.data["roles"].items():
            with self.subTest(role=role):
                self.assertIn(spec["tier"], self.data["tiers"])
                self.assertIn(spec["access"], ("read-only", "write"))

    def test_every_capability_row_is_answered_by_every_harness(self):
        for row in self.data["capabilities"]:
            with self.subTest(capability=row["key"]):
                self.assertEqual(sorted(row["values"]), sorted(HARNESSES))
                for harness, value in row["values"].items():
                    self.assertIn(value["mode"], row["modes"])
                    self.assertTrue(value["note"])

    def test_tier_pinning_row_reflects_effort_support(self):
        """The one place two independent facts could silently disagree."""
        row = next(r for r in self.data["capabilities"] if r["key"] == "tierPinning")
        for harness in HARNESSES:
            supports = self.data["harnesses"][harness]["effortSupport"]
            mode = row["values"][harness]["mode"]
            with self.subTest(harness=harness):
                self.assertEqual(
                    supports,
                    mode == "model-and-effort",
                    f"{harness}: effortSupport={supports} but tierPinning={mode}",
                )


class TestValidateRejects(unittest.TestCase):
    """Each _validate rule gets a negative test, by mutating a deepcopy."""

    @classmethod
    def setUpClass(cls):
        cls.render = _renderer()

    def _reject(self, mutate, fragment):
        config = copy.deepcopy(_config())
        mutate(config)
        with self.assertRaises(ValueError) as caught:
            self.render._validate(config)
        self.assertIn(fragment, str(caught.exception))

    def test_shipped_config_validates(self):
        self.render._validate(_config())

    def test_missing_tier(self):
        self._reject(lambda c: c["harnesses"]["claude"]["tiers"].pop("haiku"), "unanswered")

    def test_model_with_context_suffix(self):
        def mutate(c):
            c["harnesses"]["claude"]["tiers"]["opus"]["model"] = "opus[1m]"

        self._reject(mutate, "context suffix")

    def test_effort_missing_where_supported(self):
        def mutate(c):
            c["harnesses"]["claude"]["tiers"]["opus"].pop("effort")

        self._reject(mutate, "is not one of")

    def test_effort_present_where_unsupported(self):
        def mutate(c):
            c["harnesses"]["cursor"]["tiers"]["opus"]["effort"] = "high"

        self._reject(mutate, "effortSupport false")

    def test_unexplained_absent_effort(self):
        def mutate(c):
            c["harnesses"]["cursor"].pop("effortAbsentNote")

        self._reject(mutate, "effortAbsentNote")

    def test_unanswered_capability(self):
        def mutate(c):
            c["capabilities"][0]["values"].pop("codex")

        self._reject(mutate, "unanswered")

    def test_out_of_enum_mode(self):
        def mutate(c):
            c["capabilities"][0]["values"]["codex"]["mode"] = "telepathy"

        self._reject(mutate, "not one of")

    def test_native_naming_an_unknown_role(self):
        def mutate(c):
            c["harnesses"]["claude"]["natives"]["roles"]["nonesuch"] = {
                "verdict": "drop",
                "native": "x",
                "reason": "y",
            }

        self._reject(mutate, "not a real role")

    def test_native_without_a_reason(self):
        def mutate(c):
            c["harnesses"]["claude"]["natives"]["roles"]["explore"]["reason"] = ""

        self._reject(mutate, "has no reason")

    def test_skill_drop_where_packaging_cannot_express_it(self):
        """The constraint that makes `prefer` exist.

        Codex's validator hard-codes plugin_root/"skills" and Cursor's requires
        one directory, so neither can be given a reduced skill set. Recording a
        drop for them would be an exclusion the build cannot deliver.
        """

        def mutate(c):
            c["harnesses"]["codex"]["natives"]["skills"]["worktrees"] = {
                "verdict": "drop",
                "native": "x",
                "reason": "y",
            }

        self._reject(mutate, "single skills directory")

    def test_exclude_where_packaging_cannot_express_it(self):
        def mutate(c):
            c["skills"]["exclude"]["cursor"] = ["worktrees"]

        self._reject(mutate, "single skills directory")


class TestGeneratedAdapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _config()

    def _substituted(self, harness):
        natives = self.data["harnesses"][harness].get("natives", {}).get("roles", {})
        return {n for n, e in natives.items() if e["verdict"] == "drop"}

    def test_renderer_reports_no_drift(self):
        result = subprocess.run(
            [sys.executable, RENDERER, "--check"],
            cwd=REPO, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_renderer_is_idempotent(self):
        def snapshot():
            out = {}
            for root, _dirs, files in os.walk(PLUGIN):
                for name in files:
                    path = os.path.join(root, name)
                    rel = os.path.relpath(path, PLUGIN)
                    if rel.startswith(("agents", "adapters", "skills/routing/references")) or rel == "README.md":
                        with open(path, "rb") as fh:
                            out[rel] = fh.read()
            return out

        before = snapshot()
        for _ in range(2):
            subprocess.run([sys.executable, RENDERER], cwd=REPO, capture_output=True, timeout=60)
        self.assertEqual(before, snapshot())

    def test_claude_agents_pin_model_and_effort(self):
        agents = os.path.join(PLUGIN, "agents")
        tiers = self.data["harnesses"]["claude"]["tiers"]
        expected = {r for r in self.data["roles"]} - self._substituted("claude")
        self.assertEqual(
            sorted(n[:-3] for n in os.listdir(agents) if n.endswith(".md")), sorted(expected)
        )
        for role in expected:
            with open(os.path.join(agents, f"{role}.md"), encoding="utf-8") as fh:
                text = fh.read()
            row = tiers[self.data["roles"][role]["tier"]]
            with self.subTest(role=role):
                self.assertIn(f"\nmodel: {row['model']}\n", text)
                self.assertIn(f"\neffort: {row['effort']}\n", text)
                self.assertEqual(text.count("\nmodel: "), 1)
                self.assertEqual(text.count("\neffort: "), 1)

    def test_cursor_agents_inherit_and_carry_no_effort(self):
        cursor = os.path.join(PLUGIN, "adapters", "cursor", "agents")
        expected = {r for r in self.data["roles"]} - self._substituted("cursor")
        self.assertEqual(
            sorted(n[:-3] for n in os.listdir(cursor) if n.endswith(".md")), sorted(expected)
        )
        for name in os.listdir(cursor):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(cursor, name), encoding="utf-8") as fh:
                text = fh.read()
            with self.subTest(agent=name):
                self.assertIn("\nmodel: inherit\n", text)
                self.assertNotIn("\neffort:", text)

    def test_opencode_agents_match_what_the_renderer_would_emit(self):
        render = _renderer()
        with open(os.path.join(PLUGIN, "adapters", "opencode", "agents.json"), encoding="utf-8") as fh:
            shipped = fh.read()
        self.assertEqual(shipped, render._opencode_agents(self.data))

    def test_roles_carry_no_model_or_effort(self):
        """The pin lives in models.json, in exactly one place."""
        roles = os.path.join(PLUGIN, "roles")
        for name in sorted(os.listdir(roles)):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(roles, name), encoding="utf-8") as fh:
                text = fh.read()
            with self.subTest(role=name):
                self.assertNotIn("\nmodel:", text)
                self.assertNotIn("\neffort:", text)


if __name__ == "__main__":
    unittest.main()
