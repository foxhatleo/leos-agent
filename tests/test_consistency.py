"""Config-consistency lint for the self-contained plugin payload.

Run: python3 -m unittest tests.test_consistency -v
"""

import json
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
AGENTS_DIR = os.path.join(PAYLOAD, "roles")
SKILLS_DIR = os.path.join(PAYLOAD, "skills")
# Claude-only skills live in a second root so the Cursor and Codex
# manifests, which can only name a directory, cannot ship skills those
# harnesses are unable to run. Anything walking skills must walk both.
CLAUDE_SKILLS_DIR = os.path.join(PAYLOAD, "skills-claude")
SKILL_ROOTS = (SKILLS_DIR, CLAUDE_SKILLS_DIR)
WORKFLOWS_DIR = os.path.join(PAYLOAD, "workflows")
HOOKS_DIR = os.path.join(PAYLOAD, "hooks")
POLICY_FILE = os.path.join(SKILLS_DIR, "using-leo", "SKILL.md")
# The Claude-specific concretes ([1m] aliases, Agent-tool enum, Workflow-tool
# paragraph) moved out of the harness-neutral POLICY_FILE and into this
# per-harness mapping, appended by hooks/session-start.py for Claude
# sessions only. Any assertion that used to pin "[1m]" against POLICY_FILE
# now pins it here instead.
CLAUDE_MAPPING = os.path.join(SKILLS_DIR, "using-leo", "references", "claude-mapping.md")
PERSONAL_SETTINGS = os.path.join(PAYLOAD, "settings.json")
MODEL_CONFIG = os.path.join(PAYLOAD, "config", "models.json")
HOOKS_JSON = os.path.join(HOOKS_DIR, "hooks.json")

# state.py is invoked through the plugin-root variable, quoted, e.g.:
#   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/state.py"
STATE_PREFIX = "${CLAUDE_PLUGIN_ROOT}/scripts/"

# Two allowlists, because the two frontmatters accept different shapes.
#
# Agent frontmatter `model` documents exactly three forms: a bare alias, a full
# model id, or `inherit`. `opus[1m]` is NOT one of them — it is /model syntax.
# Shipping it here would be the same undocumented-assumption bet that
# "${user_config.opus_model}" was, and that one broke every spawn for two
# releases. Checked against code.claude.com/docs/en/sub-agents on 2026-07-26.
ALLOWED_AGENT_MODELS = {"haiku", "sonnet", "opus", "fable", "inherit"}
# Skill frontmatter `model` accepts the same values as /model, where the
# extended-context suffix IS valid. Same doc date.
ALLOWED_SKILL_MODELS = ALLOWED_AGENT_MODELS | {"sonnet[1m]", "opus[1m]"}

EXPECTED_AGENT_STEMS = {
    "explore", "executor", "implementer", "investigator", "reviewer",
    "expert", "planner",
}

ALLOWED_FRONTMATTER_KEYS = {"name", "description", "model", "effort", "tools", "color", "skills"}

ALLOWED_SKILL_FRONTMATTER_KEYS = {
    "name", "description", "when_to_use", "disable-model-invocation", "model", "effort",
    # Operational (user-invoked) skills carry the standard command keys.
    "allowed-tools", "argument-hint",
}

EXECUTOR_TOOL_SET = {"Read", "Grep", "Glob", "Bash", "Write", "Edit"}

# Skill dirs under plugins/leo/skills/ — portable to every harness: the
# policy skill itself plus the 9 process skills it indexes.
EXPECTED_SKILL_DIRS = {
    "using-leo",
    "debugging", "verification", "test-first", "writing-plans",
    "executing-plans", "brainstorming", "worktrees", "finishing-a-branch",
    "delegation",
}

# Skill dirs under plugins/leo/skills-claude/ — the user-facing workflow
# skills, which depend on Claude-only tools and path placeholders.
EXPECTED_CLAUDE_SKILL_DIRS = {"attach-pr", "review-pr", "resolve-ticket", "watch-review"}

# The 9 process skills the policy's "## Skill index" table must reference.
PROCESS_SKILLS = {
    "debugging", "verification", "test-first", "writing-plans",
    "executing-plans", "brainstorming", "worktrees", "finishing-a-branch",
    "delegation",
}

# Per-skill token pins: substrings each skill's body must contain, so the
# skill's load-bearing mechanics can't quietly drift away in a later edit.
PER_SKILL_TOKENS = {
    "debugging": {"Reproduce", "Localize", "Hypothesize", "Prove", "expert", "file:line", "two failures"},
    "verification": {"what changed", "checks run", "review verdict", "fresh", "falsify"},
    "test-first": {"Exemptions", "spike", "config", "failing test"},
    "writing-plans": {"TBD", "placeholder", "git rev-parse HEAD", "base ref"},
    "executing-plans": {"checkpoint", "one fix-then-re-review cycle"},
    "brainstorming": {"proportional", "blast radius", "strawman"},
    "worktrees": {"provenance", "never remove a worktree from inside", "check-ignore"},
    "finishing-a-branch": {"typed confirmation", "review verdict"},
    "delegation": {"needs-context", "blocked", "concerns", "cost-tiered-fix.js", "disjoint"},
}

# The four-state return contract only works if BOTH ends declare it: the
# orchestrator side lives in leo:delegation, and each role must be told to
# emit it. Two roles carry a deliberately narrowed set — see delegation's
# table for why — so the pin is per-role, not one uniform string.
PER_ROLE_STATUS_LINE = {
    "explore": "status: done | concerns | needs-context | blocked",
    "investigator": "status: done | concerns | needs-context | blocked",
    "planner": "status: done | concerns | needs-context | blocked",
    "implementer": "status: done | concerns | needs-context | blocked",
    "executor": "status: done | concerns | needs-context | blocked",
    # reviewer: `concerns` is a non-blocking finding and `blocked` is a
    # needs-changes verdict, so both would collide with existing vocabulary.
    "reviewer": "status: done | needs-context",
    # expert: it is the ceiling, so `blocked` has nowhere to escalate to.
    "expert": "status: done | concerns | needs-context",
}

# Skills that take untrusted third-party text (PR bodies, ticket contents)
# into a loop that can run commands. Each must say so in its own body.
INJECTION_GUARDED_SKILLS = ("attach-pr", "review-pr", "watch-review", "resolve-ticket")

# Canonical auto-escalation clause (whitespace-normalized), shared by
# expert.md and the using-leo policy skill.
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
    absorb indented continuation lines, joined with spaces. List values
    (`key:` followed by `- item` lines) are recorded with an empty string —
    good enough for key-presence checks, not for reading list contents.
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
        # blank lines, list items, or other non-continuation lines are ignored
    return result


def agent_files():
    return sorted(f for f in os.listdir(AGENTS_DIR) if f.endswith(".md"))


def agent_paths():
    return [os.path.join(AGENTS_DIR, f) for f in agent_files()]


def skill_files():
    paths = []
    for skill_root in SKILL_ROOTS:
        for root, _dirs, files in os.walk(skill_root):
            for f in files:
                if f == "SKILL.md":
                    paths.append(os.path.join(root, f))
    return sorted(paths)


def skill_path(name):
    """Locate a skill by dir name across both roots."""
    for skill_root in SKILL_ROOTS:
        candidate = os.path.join(skill_root, name, "SKILL.md")
        if os.path.isfile(candidate):
            return candidate
    raise AssertionError(f"no SKILL.md for {name} in {SKILL_ROOTS}")


def skill_dirs():
    found = set()
    for skill_root in SKILL_ROOTS:
        if not os.path.isdir(skill_root):
            continue
        found |= {
            d for d in os.listdir(skill_root)
            if os.path.isfile(os.path.join(skill_root, d, "SKILL.md"))
        }
    return found


def reference_files():
    """Per-harness mapping docs under skills/*/references/*.md (e.g.
    claude-mapping.md). Not SKILL.md files, so skill_files() never sees
    them — they need their own explicit inclusion wherever a scan claims
    to cover "everything a leo: token could live in"."""
    paths = []
    for skill_root in SKILL_ROOTS:
        for root, _dirs, files in os.walk(skill_root):
            if os.path.basename(root) != "references":
                continue
            for f in files:
                if f.endswith(".md"):
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
    # The neutral-core routing table phrases each role as prose inside a
    # table cell ("the `investigator` role", "the `planner` role (or ...)",
    # "the `executor` role", ...) rather than a bare backticked name. The
    # extraction below already tolerates that: it scans every "|"-prefixed
    # line (any table row) plus the explore special-case line for *any*
    # backticked, colon-free token, so "the `investigator` role" still
    # yields the candidate "investigator". No change needed here beyond
    # this note — verified against the neutral SKILL.md body.
    def test_backtick_agent_names_exist(self):
        with open(POLICY_FILE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

        candidates = set()
        in_skill_index = False
        for line in lines:
            if line.strip().startswith("## Skill index"):
                in_skill_index = True
            if in_skill_index:
                # The Skill index section's rows point at leo:<skill> tokens,
                # not agent names — never scan it for agent candidates.
                continue
            if line.startswith("|") or "Code location and structure-mapping" in line:
                for tok in re.findall(r"`([A-Za-z]+)`", line):
                    if ":" not in tok:
                        candidates.add(tok)

        self.assertTrue(candidates, "expected to find at least one backtick agent name")

        stems = {os.path.splitext(f)[0].lower() for f in agent_files()}
        for tok in candidates:
            with self.subTest(token=tok):
                self.assertIn(tok.lower(), stems)


class TestModelPerAgent(unittest.TestCase):
    def test_models_live_only_in_canonical_config(self):
        with open(MODEL_CONFIG, encoding="utf-8") as fh:
            config = json.load(fh)
        self.assertEqual(set(config["roles"]), EXPECTED_AGENT_STEMS)
        for f in agent_files():
            stem = os.path.splitext(f)[0].lower()
            fm = parse_frontmatter(os.path.join(AGENTS_DIR, f))
            with self.subTest(agent=stem):
                self.assertIn(stem, config["roles"])
                self.assertNotIn("model", fm)
                self.assertNotIn("effort", fm)


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
                    self.assertIn(fm["model"], ALLOWED_AGENT_MODELS)

    def test_skill_model_values(self):
        for path in skill_files():
            fm = parse_frontmatter(path)
            with self.subTest(file=os.path.relpath(path, REPO)):
                if "model" in fm:
                    self.assertIn(fm["model"], ALLOWED_SKILL_MODELS)


class TestGeneratedAgentModelShape(unittest.TestCase):
    """The rendered Claude agents are the file the model selector actually reads.

    Two properties, both violated by the 4.0-5.0.0 outage: no unresolved
    placeholder of any kind, and exactly one model line per file. An allowlist
    alone would not have caught a second, conflicting `model:` line.
    """

    def test_rendered_agents_carry_one_documented_model(self):
        agents_dir = os.path.join(PAYLOAD, "agents")
        files = sorted(f for f in os.listdir(agents_dir) if f.endswith(".md"))
        self.assertEqual({os.path.splitext(f)[0] for f in files}, EXPECTED_AGENT_STEMS)
        for f in files:
            with open(os.path.join(agents_dir, f), encoding="utf-8") as fh:
                text = fh.read()
            fm = parse_frontmatter(os.path.join(agents_dir, f))
            with self.subTest(file=f):
                self.assertNotIn("${", text, "unresolved placeholder in a generated agent")
                self.assertEqual(
                    len([ln for ln in text.splitlines() if ln.startswith("model:")]), 1
                )
                self.assertIn(fm["model"], ALLOWED_AGENT_MODELS)


class TestNoBarePins(unittest.TestCase):
    def test_skill_frontmatter_keeps_extended_context(self):
        """Skills may use [1m] and the Claude-only ones should: they run long
        review and ticket loops in the main turn. Agents deliberately may not —
        see ALLOWED_AGENT_MODELS."""
        for path in skill_files():
            fm = parse_frontmatter(path)
            if "model" not in fm or fm["model"] in {"haiku", "fable", "inherit"}:
                continue
            with self.subTest(file=os.path.relpath(path, REPO)):
                self.assertIn(fm["model"], {"sonnet[1m]", "opus[1m]"})

    def test_workflow_models_are_bare_aliases(self):
        """Inverted deliberately. This assertion used to REQUIRE the [1m] suffix
        on workflow model literals, which made the test the thing standing
        between the repo and the safe form.

        A workflow's model values are spawn-a-subagent values, same as agent
        frontmatter, and the documented shapes there are a bare alias, a full
        model id, or `inherit`. `[1m]` is /model syntax; it is valid in skill
        frontmatter and nowhere else. Betting on an undocumented model string
        already cost this repo two releases of dead agent spawns — don't re-add
        the suffix here to "restore" the larger context window.
        """
        if not os.path.isdir(WORKFLOWS_DIR):
            return
        for f in sorted(os.listdir(WORKFLOWS_DIR)):
            if not f.endswith(".js"):
                continue
            path = os.path.join(WORKFLOWS_DIR, f)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            with self.subTest(file=f):
                self.assertNotIn("[1m]", text)
                for literal in re.findall(r"model:\s*'([^']+)'", text):
                    with self.subTest(model=literal):
                        self.assertIn(literal, ALLOWED_AGENT_MODELS)


class TestExpertClauseAlignment(unittest.TestCase):
    def test_clause_present_in_both(self):
        with open(os.path.join(AGENTS_DIR, "expert.md"), encoding="utf-8") as fh:
            expert_text = _norm_ws(fh.read())
        with open(POLICY_FILE, encoding="utf-8") as fh:
            policy_text = _norm_ws(fh.read())

        clause = _norm_ws(CANONICAL_CLAUSE)
        self.assertIn(clause, expert_text)
        self.assertIn(clause, policy_text)


class TestClaudeMapping(unittest.TestCase):
    """Claude names its tier models literally — no indirection to resolve."""

    def test_mapping_file_exists(self):
        self.assertTrue(os.path.isfile(CLAUDE_MAPPING), f"missing {CLAUDE_MAPPING}")

    def test_mapping_pins_concrete_models(self):
        with open(MODEL_CONFIG, encoding="utf-8") as fh:
            claude = json.load(fh)["harnesses"]["claude"]
        with open(CLAUDE_MAPPING, encoding="utf-8") as fh:
            text = fh.read()
        for tier in ("fable", "opus", "sonnet", "haiku"):
            with self.subTest(tier=tier):
                self.assertIn(f"`{claude[tier]['model']}`", text)

    def test_no_user_config_placeholder_anywhere_in_payload(self):
        """Regression guard for the 4.0 outage.

        Claude Code does not interpolate plugin userConfig into agent
        frontmatter. Shipping "model: ${user_config.opus_model}" handed that
        literal string to the model selector, so every leo agent spawn died
        with "issue with the selected model" until it was replaced with the
        concrete alias from config/models.json.
        """
        hits = []
        for base, _dirs, files in os.walk(PAYLOAD):
            if "__pycache__" in base:
                continue
            for name in files:
                if not name.endswith((".md", ".json", ".py", ".js")):
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                if "${user_config." in text or "userConfig" in text:
                    hits.append(os.path.relpath(path, REPO))
        self.assertEqual(hits, [], f"userConfig indirection is retired; found in {hits}")

    def test_policy_file_no_longer_pins_1m(self):
        # The body is harness-neutral now: no [1m] alias literal, no
        # Agent-tool model enum, no Workflow-tool paragraph — those are
        # Claude-mapping concerns.
        with open(POLICY_FILE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("[1m]", text)


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


# Harness mapping files legitimately reference state.py through their own
# harness's root spelling; skill BODIES stay on the Claude-neutral prefix.
HARNESS_STATE_PREFIXES = (
    STATE_PREFIX,                 # ${CLAUDE_PLUGIN_ROOT}/scripts/
    "$PLUGIN_ROOT/scripts/",      # Codex
    "<plugin-root>/scripts/",     # Cursor mapping prose
)


def _state_py_prefix_matches(line, idx, prefixes=(STATE_PREFIX,)):
    """True if an allowed prefix immediately precedes `state.py` at `idx`,
    tolerating a leading double-quote right before the prefix (state.py is
    invoked as a quoted shell arg: `"${CLAUDE_PLUGIN_ROOT}/scripts/state.py"`)."""
    for prefix in prefixes:
        plen = len(prefix)
        if idx - plen >= 0 and line[idx - plen:idx] == prefix:
            return True
        if idx - plen - 1 >= 0 and line[idx - plen - 1:idx] == '"' + prefix:
            return True
    return False


class TestStatePyReferencesPrefixed(unittest.TestCase):
    """Invariant 11: state.py references must use the full CLAUDE_PLUGIN_ROOT
    prefix, except bare shorthand when an alias definition exists in the
    same file."""

    def test_every_occurrence_prefixed(self):
        walked = []
        for skill_root in SKILL_ROOTS:
            for root, dirs, files in os.walk(skill_root):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                walked.append((root, files))
        for root, files in walked:
            for fname in files:
                if fname.endswith((".pyc", ".pyo")):
                    continue
                path = os.path.join(root, fname)
                with open(path, encoding="utf-8") as fh:
                    lines = fh.readlines()

                # Check if this file has an alias definition matching
                # STATE=...${CLAUDE_PLUGIN_ROOT}/scripts/state.py...
                has_alias = any(
                    re.search(r'=[^=]*\$\{CLAUDE_PLUGIN_ROOT\}/scripts/state\.py', line)
                    for line in lines
                )

                # Harness mapping appendices speak their own harness's root
                # variable; everything else stays on the Claude-neutral prefix.
                in_references = (
                    os.sep + os.path.join("using-leo", "references") + os.sep in path
                )
                allowed = HARNESS_STATE_PREFIXES if in_references else (STATE_PREFIX,)

                for lineno, line in enumerate(lines, start=1):
                    if "state.py" not in line:
                        continue
                    # An allowed-tools permission pattern is a glob, not a path to
                    # run: `Bash(python3 */state.py *)` is exactly how the grant
                    # gets narrowed from arbitrary code execution down to this one
                    # script, so requiring a plugin-root prefix here would forbid
                    # the narrowing this suite wants.
                    if "Bash(" in line:
                        continue
                    for m in re.finditer(re.escape("state.py"), line):
                        idx = m.start()

                        has_full_prefix = _state_py_prefix_matches(line, idx, allowed)

                        # A bare mention (no leading slash) is prose naming the
                        # script, not an invocation — an invocation needs a path,
                        # and it is the path that has to resolve. Prose can't
                        # silently fail at runtime, so it needs no prefix.
                        is_bare_shorthand = idx == 0 or line[idx - 1] != "/"

                        passes = has_full_prefix or is_bare_shorthand

                        with self.subTest(file=os.path.relpath(path, REPO), line=lineno):
                            self.assertTrue(
                                passes,
                                f"{os.path.relpath(path, REPO)}:{lineno} references "
                                f"state.py without the full CLAUDE_PLUGIN_ROOT prefix",
                            )


class TestPersonalSettings(unittest.TestCase):
    def test_valid_json_with_expected_keys_and_no_hooks(self):
        with open(PERSONAL_SETTINGS, encoding="utf-8") as fh:
            settings = json.load(fh)

        expected_keys = {
            "permissions", "tui", "theme", "skipWorkflowUsageWarning", "agentPushNotifEnabled",
        }
        self.assertEqual(set(settings.keys()), expected_keys)
        self.assertNotIn("hooks", settings)


class TestReviewerExemptions(unittest.TestCase):
    def test_reviewer_mentions_both_exemptions(self):
        fm = parse_frontmatter(os.path.join(AGENTS_DIR, "reviewer.md"))
        description = fm.get("description", "")
        self.assertIn("docs", description)
        self.assertIn("dictated", description)


class TestSkillFrontmatter(unittest.TestCase):
    def test_every_skill_parses_with_expected_shape(self):
        for path in skill_files():
            with self.subTest(file=os.path.relpath(path, REPO)):
                fm = parse_frontmatter(path)  # raises on malformed fence
                parent_dir = os.path.basename(os.path.dirname(path))
                self.assertIn("name", fm)
                self.assertEqual(fm["name"], parent_dir)
                self.assertIn("description", fm)
                self.assertTrue(fm["description"].strip())
                self.assertTrue(
                    set(fm.keys()) <= ALLOWED_SKILL_FRONTMATTER_KEYS,
                    f"unexpected keys: {set(fm.keys()) - ALLOWED_SKILL_FRONTMATTER_KEYS}",
                )


class TestSkillRoster(unittest.TestCase):
    def test_skill_dir_set(self):
        self.assertEqual(skill_dirs(), EXPECTED_SKILL_DIRS | EXPECTED_CLAUDE_SKILL_DIRS)

    def test_portable_and_claude_only_roots_stay_separate(self):
        """The split is what makes the exclusion real.

        Cursor and Codex name a directory, not a list, so a Claude-only
        skill left under skills/ ships to harnesses that cannot run it.
        """
        def dirs_in(root):
            return {
                d for d in os.listdir(root)
                if os.path.isfile(os.path.join(root, d, "SKILL.md"))
            }

        self.assertEqual(dirs_in(SKILLS_DIR), EXPECTED_SKILL_DIRS)
        self.assertEqual(dirs_in(CLAUDE_SKILLS_DIR), EXPECTED_CLAUDE_SKILL_DIRS)

    def test_config_claude_only_list_matches_the_layout(self):
        """config/models.json is the declarative source; keep it honest."""
        with open(MODEL_CONFIG, encoding="utf-8") as fh:
            config = json.load(fh)
        self.assertEqual(
            set(config["skills"]["claudeOnly"]),
            EXPECTED_CLAUDE_SKILL_DIRS,
        )
        # Every excluded or Claude-only skill needs a reason, since the
        # reasons are what the generated harness mappings show the reader.
        reasons = config["skills"]["reasons"]
        named = set(config["skills"]["claudeOnly"])
        for harness_excludes in config["skills"]["exclude"].values():
            named |= set(harness_excludes)
        for name in sorted(named):
            with self.subTest(skill=name):
                self.assertIn(name, reasons)
                self.assertTrue(reasons[name].strip())


class TestCrossReferences(unittest.TestCase):
    def test_every_leo_token_resolves_to_a_skill_dir(self):
        # The leo: namespace covers both skills and agents — a skill that pins a
        # spawned subagent's type writes `leo:explore` exactly as it writes
        # `leo:delegation`, and both have to resolve or the reference is dead.
        dirs = skill_dirs() | EXPECTED_AGENT_STEMS
        # Per-harness mapping docs (e.g. claude-mapping.md) are sources too:
        # a mapping can introduce leo:<name> tokens of its own, and those
        # must resolve exactly like a token in the policy body or a skill.
        paths = agent_paths() + skill_files() + reference_files()
        if os.path.isfile(POLICY_FILE) and POLICY_FILE not in paths:
            paths.append(POLICY_FILE)

        for path in paths:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for tok in re.findall(r"leo:[a-z-]+", text):
                name = tok[len("leo:"):]
                with self.subTest(file=os.path.relpath(path, REPO), token=tok):
                    self.assertIn(name, dirs, f"{tok} in {path} does not resolve to a skill dir")


class TestNoOrphanSkills(unittest.TestCase):
    def test_every_process_skill_is_referenced_elsewhere(self):
        search_paths = agent_paths() + skill_files()
        if os.path.isfile(POLICY_FILE) and POLICY_FILE not in search_paths:
            search_paths.append(POLICY_FILE)

        contents = {}
        for path in search_paths:
            with open(path, encoding="utf-8") as fh:
                contents[path] = fh.read()

        own_skill_md = {name: skill_path(name) for name in skill_dirs()}

        # Only process skills form the cross-link DAG; operational skills
        # (review-pr, resolve-ticket, watch-review) are user-invoked entry
        # points and legitimately have no inbound leo: reference.
        for name in sorted(PROCESS_SKILLS):
            pattern = re.compile(r"leo:" + re.escape(name) + r"(?![a-z-])")
            own_path = own_skill_md.get(name)
            found = any(
                pattern.search(text)
                for path, text in contents.items()
                if path != own_path
            )
            with self.subTest(skill=name):
                self.assertTrue(found, f"leo:{name} is never referenced outside its own SKILL.md")


class TestPolicySkillIndex(unittest.TestCase):
    def test_skill_index_section_lists_process_skills(self):
        with open(POLICY_FILE, encoding="utf-8") as fh:
            text = fh.read()

        self.assertIn("## Skill index", text)
        for name in sorted(PROCESS_SKILLS):
            with self.subTest(skill=name):
                self.assertIn(f"leo:{name}", text)


class TestPerSkillTokens(unittest.TestCase):
    def test_token_pins(self):
        for name, tokens in PER_SKILL_TOKENS.items():
            path = skill_path(name)
            with self.subTest(skill=name):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                for tok in tokens:
                    with self.subTest(skill=name, token=tok):
                        self.assertIn(tok, text)


class TestFourStateContractIsDeclaredByRoles(unittest.TestCase):
    """leo:delegation's contract is unenforceable unless the roles emit it."""

    def test_every_role_declares_its_status_line(self):
        self.assertEqual(
            sorted(PER_ROLE_STATUS_LINE),
            sorted(os.path.splitext(f)[0] for f in agent_files()),
            "every role needs an explicit status-line pin (or a documented narrowing)",
        )
        for role, line in PER_ROLE_STATUS_LINE.items():
            with self.subTest(role=role):
                with open(os.path.join(AGENTS_DIR, f"{role}.md"), encoding="utf-8") as fh:
                    text = fh.read()
                self.assertIn(line, text)
                self.assertIn("leo:delegation", text)

    def test_delegation_documents_the_narrowed_roles(self):
        path = skill_path("delegation")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        # A reader of the skill alone would otherwise expect four states from
        # all seven roles.
        self.assertIn("reviewer", text)
        self.assertIn("expert", text)
        self.assertIn("SendMessage", text)


class TestUntrustedInputGuardrails(unittest.TestCase):
    """Skills that feed third-party text to a command-capable loop say so."""

    def test_guarded_skills_declare_data_not_instructions(self):
        for name in INJECTION_GUARDED_SKILLS:
            path = skill_path(name)
            with self.subTest(skill=name):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertIn("data, never instructions", text)

    def test_guarded_skills_do_not_grant_blanket_gh(self):
        # `Bash(gh *)` also grants `gh api -X POST`: arbitrary repository
        # writes, in a loop whose input is attacker-supplied.
        for name in INJECTION_GUARDED_SKILLS:
            path = skill_path(name)
            with self.subTest(skill=name):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertNotIn("Bash(gh *)", text)


class TestReadmeRoster(unittest.TestCase):
    def test_every_agent_and_skill_named_in_readme(self):
        readme = os.path.join(REPO, "README.md")
        with open(readme, encoding="utf-8") as fh:
            text = fh.read()

        for f in agent_files():
            stem = os.path.splitext(f)[0]
            with self.subTest(agent=stem):
                self.assertIn(stem, text)

        for name in skill_dirs():
            with self.subTest(skill=name):
                self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
