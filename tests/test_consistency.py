"""Structural consistency across the payload.

Rewritten for 8.0. The previous version was ~1,000 lines and pinned prose
verbatim — exact policy clauses, literal count words like "Four operational
skills", per-skill substring tables. Those failed on every edit while proving
nothing about whether the payload was coherent, so they are gone.

What survives is the set of invariants that catch a real breakage, plus three
guards that encode actual outages rather than taste:

  * the `[1m]` / `${user_config.` allowlists — both shipped and both broke
    every spawn,
  * the four-state `status:` contract, which is the only thing making a
    delegated report machine-checkable at the dispatch site,
  * the read-only-role cross-check between `access` in models.json and the
    `tools:` line in the role prompt, which is the difference between a
    read-only role and a role that merely says it is.
"""

import json
import os
import re
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
ROLES_DIR = os.path.join(PAYLOAD, "roles")
AGENTS_DIR = os.path.join(PAYLOAD, "agents")
SKILLS_DIR = os.path.join(PAYLOAD, "skills")
CLAUDE_SKILLS_DIR = os.path.join(PAYLOAD, "skills-claude")
MODEL_CONFIG = os.path.join(PAYLOAD, "config", "models.json")
SETTINGS = os.path.join(PAYLOAD, "settings.json")

# Agent frontmatter `model` documents exactly three forms: a bare alias, a full
# model id, or `inherit`. `opus[1m]` is NOT one of them — it is /model syntax,
# and shipping it killed every spawn. Skill frontmatter does accept it.
AGENT_MODELS = {"haiku", "sonnet", "opus", "inherit"}
SKILL_MODELS = AGENT_MODELS | {"haiku[1m]", "sonnet[1m]", "opus[1m]"}

ROLE_KEYS = {"name", "description", "model", "effort", "tools", "color", "skills"}
SKILL_KEYS = {
    "name", "description", "when_to_use", "model", "effort", "argument-hint",
    "arguments", "allowed-tools", "disallowed-tools", "disable-model-invocation",
    "user-invocable", "context", "agent", "background", "paths", "license",
    "compatibility", "metadata", "hooks", "shell",
}

WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

# Every dispatched role reports one of four states, so the dispatcher can route
# on it without reading prose. Two roles are deliberately narrower; see
# leo:delegation for why.
PER_ROLE_STATUS_LINE = {
    "explore": "status: done | concerns | needs-context | blocked",
    "investigator": "status: done | concerns | needs-context | blocked",
    "planner": "status: done | concerns | needs-context | blocked",
    "implementer": "status: done | concerns | needs-context | blocked",
    "executor": "status: done | concerns | needs-context | blocked",
    # reviewer: `concerns` is a non-blocking finding and `blocked` is a
    # needs-changes verdict, so both would collide with existing vocabulary.
    "reviewer": "status: done | needs-context",
    "review-lens": '"status":"done"|"needs-context"',
}

# Skills that take untrusted third-party text (PR bodies, ticket contents) into
# a loop that can run commands. Each must say so in its own body.
INJECTION_GUARDED_SKILLS = ("attach-pr", "review-pr", "watch-review", "resolve-ticket")


def _frontmatter(path):
    """Parse frontmatter without a YAML dependency, block scalars included.

    `description: >` followed by an indented paragraph is the dominant shape
    here, so a naive line split captures the literal ">" as the value and every
    content assertion silently passes against nothing.
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not match:
        raise AssertionError(f"{path} has no YAML frontmatter")

    keys = {}
    key = None
    folded = []
    for line in match.group(1).splitlines():
        top = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):(.*)$", line)
        if top:
            if key is not None:
                keys[key] = " ".join(folded).strip()
            key = top.group(1)
            value = top.group(2).strip()
            folded = [] if value in (">", "|", ">-", "|-") else [value]
        elif key is not None and line.startswith((" ", "\t", "-")):
            folded.append(line.strip())
    if key is not None:
        keys[key] = " ".join(folded).strip()
    return keys, match.group(1), match.group(2)


def parse_frontmatter(path):
    """dict-only view of the above. Imported by tests/test_opencode_plugin.py."""
    keys, _raw, _body = _frontmatter(path)
    return keys


def _config():
    with open(MODEL_CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def _skill_paths():
    out = {}
    for root in (SKILLS_DIR, CLAUDE_SKILLS_DIR):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name, "SKILL.md")
            if os.path.isfile(path):
                out[name] = path
    return out


def _payload_markdown():
    for root, _dirs, files in os.walk(PAYLOAD):
        for name in files:
            if name.endswith(".md"):
                yield os.path.join(root, name)


class TestRoles(unittest.TestCase):
    def test_role_files_match_the_config(self):
        stems = {n[:-3] for n in os.listdir(ROLES_DIR) if n.endswith(".md")}
        self.assertEqual(stems, set(_config()["roles"]))

    def test_frontmatter_is_well_formed(self):
        for name in sorted(os.listdir(ROLES_DIR)):
            if not name.endswith(".md"):
                continue
            keys, _raw, _body = _frontmatter(os.path.join(ROLES_DIR, name))
            with self.subTest(role=name):
                self.assertEqual(keys.get("name"), name[:-3])
                self.assertTrue(keys.get("description"))
                self.assertTrue(keys.get("tools"))
                self.assertLessEqual(set(keys), ROLE_KEYS)

    def test_access_agrees_with_the_declared_tools(self):
        """A read-only role that lists Write is read-only in name only."""
        config = _config()
        for role, spec in config["roles"].items():
            keys, _raw, _body = _frontmatter(os.path.join(ROLES_DIR, f"{role}.md"))
            tools = {t.strip() for t in keys["tools"].split(",") if t.strip()}
            with self.subTest(role=role):
                if spec["access"] == "read-only":
                    self.assertFalse(tools & WRITE_TOOLS, f"{role} claims read-only but lists {tools & WRITE_TOOLS}")
                else:
                    self.assertTrue(tools & WRITE_TOOLS, f"{role} is write access but lists no write tool")

    def test_every_role_declares_its_status_contract(self):
        self.assertEqual(set(PER_ROLE_STATUS_LINE), set(_config()["roles"]))
        for role, line in PER_ROLE_STATUS_LINE.items():
            with open(os.path.join(ROLES_DIR, f"{role}.md"), encoding="utf-8") as fh:
                body = fh.read()
            with self.subTest(role=role):
                self.assertIn(line, body)

    def test_dispatched_roles_point_at_the_dispatch_contract(self):
        for role in _config()["roles"]:
            if role == "review-lens":
                continue  # returns JSON to a reviewer, never dispatched directly
            with open(os.path.join(ROLES_DIR, f"{role}.md"), encoding="utf-8") as fh:
                with self.subTest(role=role):
                    self.assertIn("leo:delegation", fh.read())


class TestGeneratedAgents(unittest.TestCase):
    def test_no_unexpanded_placeholders_and_one_allowed_model(self):
        for name in sorted(os.listdir(AGENTS_DIR)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(AGENTS_DIR, name)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            keys, _raw, _body = _frontmatter(path)
            with self.subTest(agent=name):
                self.assertNotIn("${", text, "a placeholder reaches the model selector verbatim")
                self.assertEqual(text.count("\nmodel: "), 1)
                self.assertIn(keys["model"], AGENT_MODELS)


class TestSkills(unittest.TestCase):
    def test_frontmatter_is_well_formed(self):
        for name, path in _skill_paths().items():
            keys, _raw, _body = _frontmatter(path)
            with self.subTest(skill=name):
                self.assertEqual(keys.get("name"), name)
                self.assertTrue(keys.get("description"))
                self.assertLessEqual(set(keys), SKILL_KEYS, f"{name} has unknown frontmatter keys")
                if "model" in keys:
                    self.assertIn(keys["model"], SKILL_MODELS)

    def test_process_skills_declare_when_to_use(self):
        """The negative half of a trigger does more work than the positive."""
        config = _config()
        operational = set(config["skills"]["operational"]) | set(config["skills"]["claudeOnly"])
        for name, path in _skill_paths().items():
            keys, _raw, _body = _frontmatter(path)
            with self.subTest(skill=name):
                self.assertTrue(keys.get("when_to_use"), f"{name} has no when_to_use")
                if name not in operational:
                    combined = (keys["description"] + " " + keys["when_to_use"]).lower()
                    self.assertIn("not ", combined, f"{name} states no exclusion")

    def test_core_skills_exist(self):
        for name in _config()["skills"]["core"]:
            with self.subTest(skill=name):
                self.assertIn(name, _skill_paths())

    def test_every_leo_token_resolves(self):
        known = set(_skill_paths())
        for path in _payload_markdown():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for token in sorted(set(re.findall(r"leo:([a-z][a-z-]*)", text))):
                with self.subTest(path=os.path.relpath(path, PAYLOAD), token=token):
                    self.assertIn(token, known)

    def test_no_skill_is_an_orphan(self):
        """Indexed and shipped must agree in both directions.

        A skill nothing references is unreachable; an index row pointing at a
        skill that does not ship reads as available until someone invokes it.
        """
        known = set(_skill_paths())
        referenced = set()
        for path in _payload_markdown():
            owner = os.path.basename(os.path.dirname(path))
            with open(path, encoding="utf-8") as fh:
                for token in re.findall(r"leo:([a-z][a-z-]*)", fh.read()):
                    if token != owner:
                        referenced.add(token)
        # routing indexes itself only by being the entry point.
        for name in sorted(known - referenced - {"routing"}):
            with self.subTest(skill=name):
                self.fail(f"{name} is shipped but referenced from nowhere else")

    def test_untrusted_input_skills_say_so(self):
        paths = _skill_paths()
        for name in INJECTION_GUARDED_SKILLS:
            with open(paths[name], encoding="utf-8") as fh:
                with self.subTest(skill=name):
                    self.assertIn("data, never instructions", fh.read())

    def test_no_blanket_command_grants(self):
        """A wildcard `gh` grant also grants `gh api -X POST`."""
        for name, path in _skill_paths().items():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for blanket in ("- Bash(gh *)", "- Bash(git *)", "- Bash(python3 *)"):
                with self.subTest(skill=name, grant=blanket):
                    self.assertNotIn(blanket, text)


class TestPolicySkills(unittest.TestCase):
    """The two skills that replaced the injected policy."""

    def setUp(self):
        with open(os.path.join(SKILLS_DIR, "routing", "SKILL.md"), encoding="utf-8") as fh:
            self.routing = fh.read()

    def test_routing_indexes_the_process_skills(self):
        config = _config()
        indexed = set(re.findall(r"leo:([a-z][a-z-]*)", self.routing))
        operational = set(config["skills"]["operational"]) | set(config["skills"]["claudeOnly"])
        expected = set(_skill_paths()) - {"routing"} - operational
        missing = expected - indexed
        self.assertFalse(missing, f"routing's skill index omits {sorted(missing)}")

    def test_routing_names_the_operational_skills(self):
        config = _config()
        for name in config["skills"]["operational"] + config["skills"]["claudeOnly"]:
            with self.subTest(skill=name):
                self.assertIn(f"leo:{name}", self.routing)

    def test_no_trace_of_policy_injection(self):
        """8.0 regression guard: the policy is a skill, not an injected block."""
        for gone in ("SessionStart", "<leo-policy>", "session bootstrap", "injects this body"):
            with self.subTest(token=gone):
                self.assertNotIn(gone, self.routing)

    def test_routing_defers_harness_detail_to_the_reference(self):
        self.assertIn("references/harnesses.md", self.routing)

    def test_routing_stays_within_budget(self):
        body = self.routing.split("---", 2)[2]
        self.assertLess(len(body.split()), 1200, "routing's body has grown past its budget")


class TestNoKnownFootguns(unittest.TestCase):
    """Two allowlists encoding two shipped outages."""

    def test_no_user_config_placeholder_anywhere(self):
        for root, _dirs, files in os.walk(PAYLOAD):
            for name in files:
                if not name.endswith((".md", ".json", ".py", ".js")):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
                with self.subTest(path=os.path.relpath(path, PAYLOAD)):
                    self.assertNotIn("${user_config.", text)
                    self.assertNotIn("userConfig", text)

    def test_workflows_use_bare_aliases_only(self):
        workflows = os.path.join(PAYLOAD, "workflows")
        for name in sorted(os.listdir(workflows)):
            if not name.endswith(".js"):
                continue
            with open(os.path.join(workflows, name), encoding="utf-8") as fh:
                text = fh.read()
            with self.subTest(workflow=name):
                for model in re.findall(r"model:\s*'([^']+)'", text):
                    self.assertIn(model, AGENT_MODELS)


class TestSettings(unittest.TestCase):
    def test_reference_settings_declare_no_hooks(self):
        """settings.json is a suggestion for Leo's own machine, not a component.

        A `hooks` key here would be loaded on top of the plugin's own manifest.
        """
        with open(SETTINGS, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertNotIn("hooks", data)


if __name__ == "__main__":
    unittest.main()
