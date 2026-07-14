"""Config-consistency lint: invariants tying together agents/, skills/,
CLAUDE.md, settings.json, and workflows/. Stdlib unittest only.

Run: python3 -m unittest tests.test_consistency -v
"""

import json
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_DIR = os.path.join(REPO, "claude", "agents")
SKILLS_DIR = os.path.join(REPO, "claude", "skills")
WORKFLOWS_DIR = os.path.join(REPO, "claude", "workflows")
CLAUDE_MD = os.path.join(REPO, "claude", "CLAUDE.md")
SETTINGS_JSON = os.path.join(REPO, "claude", "settings.json")
HOOKS_DIR = os.path.join(REPO, "claude", "hooks")

STATE_PREFIX = "${LEOS_AGENT_PATH:-$HOME/.leos-agent}/claude/scripts/"

ALLOWED_MODELS = {"haiku", "sonnet[1m]", "opus[1m]", "fable", "inherit"}

EXPECTED_AGENT_STEMS = {
    "explore", "executor", "implementer", "investigator", "reviewer",
    "expert", "planner",
}

EXPECTED_MODEL_BY_AGENT = {
    "investigator": "opus[1m]",
    "planner": "opus[1m]",
    "reviewer": "opus[1m]",
    "implementer": "sonnet[1m]",
    "executor": "haiku",
    "explore": "haiku",
    "expert": "fable",
}

ALLOWED_FRONTMATTER_KEYS = {"name", "description", "model", "effort", "tools", "color"}

EXECUTOR_TOOL_SET = {"Read", "Grep", "Glob", "Bash", "Write", "Edit"}

# Canonical auto-escalation clause (whitespace-normalized), shared by
# expert.md and CLAUDE.md.
CANONICAL_CLAUSE = (
    "an opus-tier agent failed twice on the same question, or returned low "
    "confidence that a re-run with more evidence did not raise and the task "
    "cannot reach a verdict without arbitration — a single low-confidence "
    "result, or low confidence only waiting on still-gatherable evidence, "
    "never qualifies"
)


def _norm_ws(s):
    return re.sub(r"\s+", " ", s).strip()


def parse_frontmatter(path):
    """Tiny YAML-ish frontmatter parser: text between leading '---' fences.

    Parses column-0 `key:` lines; block scalars (`key: >` or `key: |`)
    absorb indented continuation lines, joined with spaces.
    Returns dict[str, str].
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path}: no leading '---' fence")

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise ValueError(f"{path}: no closing '---' fence")

    body = lines[1:end]
    result = {}
    key = None
    is_block = False
    for line in body:
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m and (line == line.lstrip()):
            key = m.group(1)
            val = m.group(2).strip()
            if val in (">", "|", ">-", "|-"):
                result[key] = ""
                is_block = True
            else:
                result[key] = val
                is_block = False
        elif key is not None and is_block and line.strip():
            result[key] = (result[key] + " " + line.strip()).strip()
        # blank lines or non-continuation lines are ignored
    return result


def agent_files():
    return sorted(f for f in os.listdir(AGENTS_DIR) if f.endswith(".md"))


def skill_files():
    paths = []
    for root, _dirs, files in os.walk(SKILLS_DIR):
        for f in files:
            if f == "SKILL.md":
                paths.append(os.path.join(root, f))
    return sorted(paths)


class TestAgentRoster(unittest.TestCase):
    def test_agent_file_set(self):
        stems = {os.path.splitext(f)[0].lower() for f in agent_files()}
        self.assertEqual(stems, EXPECTED_AGENT_STEMS)


class TestFrontmatterNameMatchesFilename(unittest.TestCase):
    def test_name_matches_stem(self):
        for f in agent_files():
            path = os.path.join(AGENTS_DIR, f)
            fm = parse_frontmatter(path)
            stem = os.path.splitext(f)[0]
            with self.subTest(file=f):
                self.assertIn("name", fm)
                self.assertEqual(fm["name"].lower(), stem.lower())


class TestRoutingTableAgentsResolve(unittest.TestCase):
    def test_backtick_agent_names_exist(self):
        with open(CLAUDE_MD, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

        candidates = set()
        for line in lines:
            if line.startswith("|") or "Code location and structure-mapping" in line:
                for tok in re.findall(r"`([A-Za-z]+)`", line):
                    candidates.add(tok)

        self.assertTrue(candidates, "expected to find at least one backtick agent name")

        stems = {os.path.splitext(f)[0].lower() for f in agent_files()}
        for tok in candidates:
            with self.subTest(token=tok):
                self.assertIn(tok.lower(), stems)


class TestModelPerAgent(unittest.TestCase):
    def test_model_matches_tier(self):
        for f in agent_files():
            stem = os.path.splitext(f)[0].lower()
            if stem not in EXPECTED_MODEL_BY_AGENT:
                continue
            fm = parse_frontmatter(os.path.join(AGENTS_DIR, f))
            with self.subTest(agent=stem):
                self.assertEqual(fm.get("model"), EXPECTED_MODEL_BY_AGENT[stem])


class TestAgentFrontmatterKeySubset(unittest.TestCase):
    def test_keys_subset(self):
        for f in agent_files():
            fm = parse_frontmatter(os.path.join(AGENTS_DIR, f))
            with self.subTest(file=f):
                self.assertTrue(
                    set(fm.keys()) <= ALLOWED_FRONTMATTER_KEYS,
                    f"{f} has unexpected keys: {set(fm.keys()) - ALLOWED_FRONTMATTER_KEYS}",
                )


class TestModelValueAllowlist(unittest.TestCase):
    def test_agent_model_values(self):
        for f in agent_files():
            fm = parse_frontmatter(os.path.join(AGENTS_DIR, f))
            with self.subTest(file=f):
                if "model" in fm:
                    self.assertIn(fm["model"], ALLOWED_MODELS)

    def test_skill_model_values(self):
        for path in skill_files():
            fm = parse_frontmatter(path)
            with self.subTest(file=os.path.relpath(path, REPO)):
                if "model" in fm:
                    self.assertIn(fm["model"], ALLOWED_MODELS)


class TestNoBarePins(unittest.TestCase):
    def test_agents_and_skills_frontmatter(self):
        paths = [os.path.join(AGENTS_DIR, f) for f in agent_files()] + skill_files()
        for path in paths:
            fm = parse_frontmatter(path)
            with self.subTest(file=os.path.relpath(path, REPO)):
                if "model" in fm:
                    self.assertNotIn(fm["model"], {"opus", "sonnet"})

    def test_workflows_no_bare_literals(self):
        if not os.path.isdir(WORKFLOWS_DIR):
            return
        for f in sorted(os.listdir(WORKFLOWS_DIR)):
            if not f.endswith(".js"):
                continue
            path = os.path.join(WORKFLOWS_DIR, f)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            with self.subTest(file=f):
                if "// [1m]-fallback" in text:
                    continue
                self.assertNotRegex(text, r"model:\s*'opus'")
                self.assertNotRegex(text, r"model:\s*'sonnet'")
                self.assertNotRegex(text, r"(?<![\w-])'opus'(?!\[1m\])")
                self.assertNotRegex(text, r"(?<![\w-])'sonnet'(?!\[1m\])")


class TestExpertClauseAlignment(unittest.TestCase):
    def test_clause_present_in_both(self):
        with open(os.path.join(AGENTS_DIR, "expert.md"), encoding="utf-8") as fh:
            expert_text = _norm_ws(fh.read())
        with open(CLAUDE_MD, encoding="utf-8") as fh:
            claude_text = _norm_ws(fh.read())

        clause = _norm_ws(CANONICAL_CLAUSE)
        self.assertIn(clause, expert_text)
        self.assertIn(clause, claude_text)


class TestExplicitToolsDeclared(unittest.TestCase):
    def test_readonly_agents_declare_tools(self):
        for stem in ("investigator", "reviewer", "expert", "explore", "planner"):
            matches = [f for f in agent_files() if os.path.splitext(f)[0].lower() == stem]
            self.assertEqual(len(matches), 1, f"expected exactly one file for {stem}")
            fm = parse_frontmatter(os.path.join(AGENTS_DIR, matches[0]))
            with self.subTest(agent=stem):
                self.assertIn("tools", fm)
                self.assertTrue(fm["tools"].strip())


class TestExecutorImplementerTools(unittest.TestCase):
    def test_tools_subset(self):
        for stem in ("executor", "implementer"):
            matches = [f for f in agent_files() if os.path.splitext(f)[0].lower() == stem]
            self.assertEqual(len(matches), 1, f"expected exactly one file for {stem}")
            fm = parse_frontmatter(os.path.join(AGENTS_DIR, matches[0]))
            with self.subTest(agent=stem):
                self.assertIn("tools", fm)
                tokens = {t.strip() for t in fm["tools"].split(",") if t.strip()}
                self.assertTrue(tokens, f"{stem} declares no tools")
                self.assertTrue(
                    tokens <= EXECUTOR_TOOL_SET,
                    f"{stem} tools {tokens} not subset of {EXECUTOR_TOOL_SET}",
                )


class TestStatePyReferencesPrefixed(unittest.TestCase):
    """Invariant 11: state.py references must use the full LEOS_AGENT_PATH prefix,
    except bare shorthand when an alias definition (STATE=...) exists in the same file."""

    def test_every_occurrence_prefixed(self):
        for root, dirs, files in os.walk(SKILLS_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fname in files:
                if fname.endswith((".pyc", ".pyo")):
                    continue
                path = os.path.join(root, fname)
                with open(path, encoding="utf-8") as fh:
                    lines = fh.readlines()

                # Check if this file has an alias definition matching STATE="...state.py..."
                has_alias = any(
                    re.search(r'=\s*"[^"]*\$\{LEOS_AGENT_PATH[^}]*\}/claude/scripts/state\.py', line)
                    for line in lines
                )

                for lineno, line in enumerate(lines, start=1):
                    if "state.py" not in line:
                        continue
                    for m in re.finditer(re.escape("state.py"), line):
                        idx = m.start()
                        prefix_start = idx - len(STATE_PREFIX)

                        # Check if this occurrence has the full prefix
                        has_full_prefix = prefix_start >= 0 and line[prefix_start:idx] == STATE_PREFIX

                        # Check if this is bare shorthand (not /state.py)
                        is_bare_shorthand = idx == 0 or line[idx - 1] != "/"

                        # Pass if: full prefix OR (has alias definition AND bare shorthand)
                        passes = has_full_prefix or (has_alias and is_bare_shorthand)

                        with self.subTest(file=os.path.relpath(path, REPO), line=lineno):
                            self.assertTrue(
                                passes,
                                f"{os.path.relpath(path, REPO)}:{lineno} references "
                                f"state.py without the full LEOS_AGENT_PATH prefix",
                            )


class TestSettingsJson(unittest.TestCase):
    def test_settings_and_hook(self):
        with open(SETTINGS_JSON, encoding="utf-8") as fh:
            settings = json.load(fh)

        pre_tool_use = settings.get("hooks", {}).get("PreToolUse", [])
        self.assertTrue(pre_tool_use, "expected hooks.PreToolUse to be non-empty")

        found_bash_guard = False
        for entry in pre_tool_use:
            self.assertEqual(entry.get("matcher"), "Bash")
            for hook in entry.get("hooks", []):
                self.assertIsInstance(hook.get("timeout"), int)
                if "bash-guard.py" in hook.get("command", ""):
                    found_bash_guard = True

        self.assertTrue(found_bash_guard, "expected a hook command referencing bash-guard.py")
        self.assertTrue(os.path.isfile(os.path.join(HOOKS_DIR, "bash-guard.py")))


class TestReviewerExemptions(unittest.TestCase):
    def test_reviewer_mentions_both_exemptions(self):
        with open(os.path.join(AGENTS_DIR, "reviewer.md"), encoding="utf-8") as fh:
            fm = parse_frontmatter(os.path.join(AGENTS_DIR, "reviewer.md"))
        description = fm.get("description", "")
        self.assertIn("docs", description)
        self.assertIn("dictated", description)


if __name__ == "__main__":
    unittest.main()
