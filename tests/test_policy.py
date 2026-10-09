"""Cost and routing properties the plugin promises to users."""

import importlib.util
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


WORKERS = {"leo-cheap": "haiku", "leo-standard": "sonnet", "leo-premium": "opus", "leo-parent": "inherit"}

# Plugin-agent frontmatter Claude Code reads (plugins reference, "Frontmatter
# fields in plugin agents"). It ignores any other key without an error, so a
# misspelled `max_turns` would silently ship an uncapped worker.
CLAUDE_PLUGIN_AGENT_FIELDS = {
    "name", "description", "model", "effort", "maxTurns", "tools", "disallowedTools", "skills",
    "memory", "background", "omitClaudeMd", "isolation", "color", "experimental",
}

# OpenCode moves every agent key outside this set into `options`, which reach
# the provider as model options (packages/core/src/v1/config/agent.ts).
OPENCODE_AGENT_KEYS = {
    "name", "model", "variant", "prompt", "description", "temperature", "top_p", "mode", "hidden",
    "color", "steps", "maxSteps", "options", "permission", "disable", "tools",
}


def frontmatter_keys(text):
    fm = text.split("---", 2)[1]
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(?m)^([A-Za-z_][\w-]*):(.*)$", fm)}


class TestRouting(unittest.TestCase):
    def test_codex_profiles_leave_selection_to_the_spawn(self):
        for name in WORKERS:
            text = (ROOT / "payload" / "codex-agents" / f"{name}.toml").read_text(encoding="utf-8")
            with self.subTest(profile=name):
                self.assertNotIn('model =', text)
                self.assertNotIn('model_reasoning_effort =', text)

    def test_claude_workers_bake_their_tier_model_and_cannot_delegate(self):
        # The Claude profiles bake the tier's model into the agent type, so a
        # brief that names the profile cannot silently inherit the parent model.
        for name, model in WORKERS.items():
            fields = frontmatter_keys((ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8"))
            with self.subTest(profile=name):
                self.assertEqual(fields["model"], model)
                self.assertEqual(fields["disallowedTools"], "Agent")
                tools = {t.strip() for t in fields["tools"].split(",")}
                self.assertTrue({"Read", "Edit", "Write", "Bash"} <= tools)
                self.assertNotIn("Agent", tools)

    def test_claude_workers_cap_turns_no_lower_than_the_tier_below(self):
        caps = []
        for name in WORKERS:
            value = frontmatter_keys((ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8")).get("maxTurns")
            with self.subTest(profile=name):
                self.assertIsNotNone(value, "an absent cap means the worker can run unbounded")
                self.assertRegex(value, r"^[1-9][0-9]*$")
            caps.append(int(value))
        self.assertEqual(caps, sorted(caps), "a higher tier must not get fewer turns than a lower one")

    def test_claude_agent_frontmatter_uses_only_keys_claude_code_reads(self):
        for path in sorted((ROOT / "agents").glob("*.md")):
            with self.subTest(agent=path.stem):
                keys = set(frontmatter_keys(path.read_text(encoding="utf-8")))
                self.assertEqual(keys - CLAUDE_PLUGIN_AGENT_FIELDS, set())

    def test_claude_only_frontmatter_never_reaches_other_harness_agents(self):
        spec = importlib.util.spec_from_file_location("leo_install_policy_test", ROOT / "scripts" / "leo-install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for name in installer.CODEX_AGENTS:
            for harness in ("cursor", "opencode"):
                with self.subTest(agent=name, harness=harness):
                    keys = set(frontmatter_keys(installer.native_agent(ROOT, name, harness, {})))
                    self.assertNotIn("maxTurns", keys)
                    if harness == "opencode":
                        self.assertEqual(keys - OPENCODE_AGENT_KEYS, set())

    def test_retired_alias_names_still_route_without_profile_files(self):
        # Old briefs and dispatch-log rows keep their tier after the profiles go.
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import routing_engine
        finally:
            sys.path.pop(0)
        for alias, tier in (("leo-runner", "cheap"), ("leos-agent:leo-executor", "standard")):
            with self.subTest(alias=alias):
                self.assertFalse((ROOT / "agents" / f"{alias.split(':')[-1]}.md").exists())
                self.assertEqual(routing_engine.tier_for(alias), tier)

    def test_escalation_contract_tokens_are_present_wherever_they_are_parsed_from(self):
        # The guard parses the brief header and the observer parses the worker's
        # closing line. The policy must teach the parent both tokens, and every
        # worker body must ask for the closing line on every harness.
        policy = (ROOT / "rules" / "preferences.md").read_text(encoding="utf-8")
        self.assertIn("Escalation from <tier>:", policy)
        self.assertIn("Result: escalate", policy)
        for path in sorted((ROOT / "agents").glob("leo-*.md")):
            with self.subTest(agent=path.stem):
                self.assertIn("Result: done|partial|blocked|escalate", path.read_text(encoding="utf-8"))
        self.assertIn("Result: done|partial|blocked|escalate",
                      (ROOT / "skills" / "review-pr" / "reference" / "procedure.md").read_text(encoding="utf-8"))

    def test_clean_fork_flag_is_stated_everywhere_spawning_is_described(self):
        # A harness flag, not prose: every file that tells the model to spawn has
        # to name it, or one spawn path silently inherits the parent's history.
        # Assert the token only — the sentence around it is free to be reworded.
        sources = (
            ROOT / "rules" / "preferences.md",
            ROOT / "skills" / "review-pr" / "SKILL.md",
            ROOT / "skills" / "review-pr" / "reference" / "procedure.md",
        )
        for path in sources:
            with self.subTest(source=path.relative_to(ROOT).as_posix()):
                self.assertIn('fork_turns="none"', path.read_text(encoding="utf-8"))


class TestInvocationPolicy(unittest.TestCase):
    def test_explicit_only_portable_skills_have_codex_policy(self):
        for name in ("doctor", "handoff", "install", "tune-routing"):
            path = ROOT / "skills" / name / "agents" / "openai.yaml"
            with self.subTest(skill=name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("interface:\n", text)
                self.assertIn("  display_name:", text)
                self.assertIn("  short_description:", text)
                self.assertIn("policy:\n  allow_implicit_invocation: false\n", text)

    def test_implicit_skills_are_not_accidentally_disabled(self):
        for name in ("handon", "review-pr"):
            path = ROOT / "skills" / name / "agents" / "openai.yaml"
            with self.subTest(skill=name):
                self.assertFalse(path.exists())


class TestPluginRootConvention(unittest.TestCase):
    """Every skill resolves the plugin root; none builds a path from the env var.

    Claude Code substitutes `${CLAUDE_PLUGIN_ROOT}` inline in skill bodies, but
    the other five harnesses read the same files literally, and Claude does not
    export it to the Bash tool or to Monitor processes. A skill that hardcodes
    it into a command ships one that runs `python3 /scripts/….py` there, and a
    model that tries to expand it itself has nothing to expand it from but the
    directory the SKILL.md was read out of -- which is never the plugin root.
    """

    DIRS = ("skills", "skills-claude", "commands", "commands-claude")

    def docs(self):
        for name in self.DIRS:
            yield from sorted((ROOT / name).rglob("*.md"))

    def test_the_env_var_is_never_used_as_a_path_prefix(self):
        # Naming $CLAUDE_PLUGIN_ROOT in a resolution sentence is fine and
        # expected; building a path out of it is the bug.
        for path in self.docs():
            with self.subTest(doc=path.relative_to(ROOT).as_posix()):
                self.assertIsNone(
                    re.search(r"CLAUDE_PLUGIN_ROOT\}?/", path.read_text(encoding="utf-8")),
                    "use <plugin-root>/scripts/… and resolve it first",
                )

    def test_every_script_invocation_uses_the_placeholder(self):
        for path in self.docs():
            text = path.read_text(encoding="utf-8")
            for prefix in re.findall(r"(?m)^\s*python3\s+\"?(\S+)/scripts/\S+\.py", text):
                with self.subTest(doc=path.relative_to(ROOT).as_posix(), prefix=prefix):
                    self.assertEqual(prefix.lstrip('"'), "<plugin-root>")


class TestStaticPromptBudget(unittest.TestCase):
    def test_committed_context_ceilings(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "measure_context.py"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tune_routing_costs_no_always_loaded_bytes(self):
        # The skill's whole premise is that configuring the cheap tier must not
        # itself cost always-loaded context. Assert it directly rather than
        # relying on the aggregate ceilings happening to have headroom.
        spec = importlib.util.spec_from_file_location(
            "measure_policy_test", ROOT / "scripts" / "measure_context.py"
        )
        measure = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(measure)
        path = ROOT / "skills" / "tune-routing" / "SKILL.md"
        fm, _ = measure.frontmatter(path)
        self.assertFalse(measure.codex_implicit(path, fm))
        self.assertFalse(measure.claude_implicit(path, fm))

    def test_skill_descriptions_stay_single_line(self):
        for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
            text = path.read_text(encoding="utf-8")
            fm = text.split("---", 2)[1]
            match = re.search(r"(?m)^description:\s*(.+)$", fm)
            with self.subTest(skill=path.parent.name):
                self.assertIsNotNone(match)
                self.assertLessEqual(len(match.group(1).encode("utf-8")), 260)


if __name__ == "__main__":
    unittest.main()
