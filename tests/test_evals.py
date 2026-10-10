"""The eval suite under evals/ grades what it says it grades.

`claude plugin eval` has no free validation mode, and every run of it is a paid
model call, so this module is the suite's offline gate. It holds each case to
the documented case format, re-derives each correct answer from the fixture
files beside it, and runs every grader against input shaped the way Claude Code
produces it: Agent tool input as compact JSON, both as the model wrote it and as
the dispatch guard corrects it, and a session trace with one JSON message per
line.

The runner evaluates grader regexes as JavaScript. Python's `re` agrees with it
on the subset the patterns are held to here, so that subset is enforced as well.
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
sys.path.insert(0, str(ROOT / "scripts"))
import emit_payload  # noqa: E402
import outcome  # noqa: E402
import pricing  # noqa: E402
import routing_engine  # noqa: E402

_CONFIG_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "PI_CODING_AGENT_DIR",
               "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "XDG_CONFIG_HOME", "LEOS_AGENT_LOCAL_PATH")
_SANDBOX = None
_ENV = None


def setUpModule():
    global _SANDBOX, _ENV
    _SANDBOX = tempfile.TemporaryDirectory()
    env = {k: v for k, v in os.environ.items() if k not in _CONFIG_ENV}
    env.update(HOME=_SANDBOX.name, LEOS_AGENT_LOCAL_PATH=os.path.join(_SANDBOX.name, "local"),
               LEOS_AGENT_PRICE_REFRESH="off")
    _ENV = mock.patch.dict(os.environ, env, clear=True)
    _ENV.start()


def tearDownModule():
    _ENV.stop()
    _SANDBOX.cleanup()


# The documented case format. An unknown prompt.md key is a load error there.
PROMPT_KEYS = {"schema_version", "name", "description", "tags", "plugins", "runs", "expected_outcome",
               "model", "max_turns", "timeout_seconds", "allowed_tools", "append_system_prompt", "env"}
CASE_KEYS = {"schema_version", "name", "description", "tags", "plugins", "runs", "expected_outcome",
             "execution", "context", "graders"}
CONTEXT_KEYS = {"scaffold_script", "history_file", "add_dirs"}
SCAFFOLD = "stage-fixtures.sh"
# Case files that are not fixtures: the case definition and its graders.
CASE_FILES = {"case.yaml", "prompt.md", "graders", SCAFFOLD}


def fixture_dirs(case_dir):
    return sorted(p.name for p in case_dir.iterdir() if p.name not in CASE_FILES)


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
# case.yaml's `execution:` takes the prompt.md run fields plus the prompt.
EXECUTION_KEYS = {"prompt", "model", "max_turns", "timeout_seconds", "allowed_tools", "append_system_prompt", "env"}
ENV_KEY_RE = re.compile(r"EVAL_[A-Z0-9_]*")
GRADER_KEYS = {"type", "weight", "arm"}
TYPE_KEYS = {
    "regex": {"pattern", "flags", "match", "target"},
    "tool_used": {"tool", "input_match", "min", "max"},
    "tool_order": {"before", "after"},
    "file_exists": {"path", "exists"},
    "llm": {"criteria", "focus"},
    "baseline": {"baseline_file", "criteria"},
}
JUDGE_TYPES = {"llm", "baseline"}
TARGETS = {"last_message", "trace", "files", "mock_calls"}
# What a run grants from allowed_tools without an --allow-tools grant.
READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "NotebookRead", "Skill", "AskUserQuestion", "Agent", "TodoWrite",
                   "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Constructs where JavaScript and Python regexes disagree, or that one lacks.
FORBIDDEN = (
    (re.compile(r"\(\?[A-Za-z]"), "inline flags and Python-only groups"),
    (re.compile(r"\(\?<"), "lookbehind and named groups"),
    (re.compile(r"\(\?[>#]"), "atomic groups and comments"),
    (re.compile(r"\\[AZzGhHkKpPQEXR]"), "escapes the two engines read differently"),
    (re.compile(r"[*+?}]\+"), "possessive quantifiers"),
)
PY_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


# ---- a strict reader for the YAML subset the suite is written in -----------

def scalar(text, where):
    text = text.strip()
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'") or "'" in text[1:-1].replace("''", ""):
            raise ValueError(f"{where}: malformed single-quoted scalar {text!r}")
        return text[1:-1].replace("''", "'")
    if text.startswith('"'):
        if len(text) < 2 or not text.endswith('"') or '"' in text[1:-1] or "\\" in text:
            raise ValueError(f"{where}: double-quoted scalars here must be plain text: {text!r}")
        return text[1:-1]
    if text.startswith("["):
        if not text.endswith("]"):
            raise ValueError(f"{where}: unterminated flow list {text!r}")
        inner = text[1:-1].strip()
        return [scalar(part, where) for part in inner.split(",")] if inner else []
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text in ("true", "false"):
        return text == "true"
    if text.lower() in ("on", "off", "yes", "no", "y", "n"):
        # A boolean to YAML 1.1 readers and a string to 1.2 ones: an env value
        # written bare could reach the run as false, or fail its string schema.
        raise ValueError(f"{where}: quote {text!r}; YAML readers disagree on whether it is a boolean")
    if not text or text[0] in "{}&*!|>%@`#?:-,]" or ": " in text or " #" in text or text.endswith(":"):
        raise ValueError(f"{where}: unsupported or ambiguous YAML scalar {text!r}")
    return text


def parse_yaml(text, where):
    root = {}
    stack = [(-1, root)]
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if raw[indent:indent + 1] == "\t":
            raise ValueError(f"{where}:{number}: tab indentation")
        match = re.fullmatch(r"([A-Za-z_][\w-]*):(?:[ ]+(.*))?", raw.strip())
        if not match:
            raise ValueError(f"{where}:{number}: unsupported YAML line {raw!r}")
        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        key, value = match.group(1), match.group(2)
        if key in parent:
            raise ValueError(f"{where}:{number}: duplicate key {key!r}")
        if value is None or not value.strip():
            parent[key] = {}
            stack.append((indent, parent[key]))
        else:
            parent[key] = scalar(value, f"{where}:{number}")
    return root


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n?(.*)\Z", text, re.DOTALL)
    if not match:
        raise ValueError(f"{path.relative_to(ROOT)}: no frontmatter")
    return parse_yaml(match.group(1), str(path.relative_to(ROOT))), match.group(2)


def load_suite():
    cases = {}
    for case_dir in sorted(p for p in EVALS.iterdir() if p.is_dir() and p.name != "results"):
        meta, body = frontmatter(case_dir / "prompt.md")
        case_yaml = parse_yaml((case_dir / "case.yaml").read_text(encoding="utf-8"),
                               str((case_dir / "case.yaml").relative_to(ROOT)))
        graders = {p.stem: frontmatter(p) for p in sorted((case_dir / "graders").glob("*.md"))}
        cases[case_dir.name] = {"dir": case_dir, "meta": meta, "body": body, "case": case_yaml, "graders": graders}
    return cases


# ---- grader semantics, as documented -----------------------------------------

def js_json(value):
    """JSON.stringify: compact separators, non-ASCII left as is."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def compiled(grader):
    flags = 0
    for flag in str(grader.get("flags", "")):
        flags |= PY_FLAGS[flag]
    return re.compile(grader["pattern"], flags)


def regex_passes(grader, text):
    hits = sum(1 for _ in compiled(grader).finditer(text))
    mode = grader.get("match", "contains")
    if mode == "contains":
        return hits > 0
    if mode == "not_contains":
        return hits == 0
    return hits == int(re.fullmatch(r"count:(\d+)", mode).group(1))


def tool_used_passes(grader, calls):
    """calls: [(tool_name, input_dict)] as the transcript records them."""
    matcher = re.compile(grader["input_match"]) if grader.get("input_match") else None
    hits = sum(1 for tool, args in calls
               if tool == grader["tool"] and (matcher is None or matcher.search(js_json(args))))
    high = grader.get("max")
    return hits >= grader.get("min", 1) and (high is None or hits <= high)


def scored_in_both_arms(grader):
    if grader.get("arm") == "with-only":
        return False
    return not (grader["type"] == "tool_used" and grader.get("tool") == "Skill") or grader.get("arm") == "both"


def agent_input(subagent_type=None, model=None, prompt="Read every file under the fixture directory and report."):
    args = {"description": "Scan the fixture files", "prompt": prompt}
    if subagent_type:
        args["subagent_type"] = subagent_type
    if model:
        args["model"] = model
    return args


def guard_view(args, parent="claude-sonnet-5-5"):
    """The input after the Claude dispatch guard runs: corrected, or unchanged."""
    catalog = json.loads(pricing.BUNDLED.read_text())
    result = routing_engine.route("claude", "Agent", args, parent, config={}, catalog=catalog)
    return result["updated_input"] or args


def final_message(items, lead="I read every file and checked the result.\n\n"):
    return lead + "ANSWER: " + ", ".join(items)


# ---- fixture ground truth ----------------------------------------------------
# Each prose fixture states its facts in exactly one sentence from these banks,
# so the answer is fixed by construction and re-derived here, out of the agent's
# sight: a run cannot read the eval directory beyond the fixture itself.

CAUSE = {
    "config": (
        "A feature-flag change enabled the new cache path in every region at once instead of the canary region only.",
        "A settings push lowered the connection-pool limit in the runtime configuration from 200 to 20.",
        "The load-balancer health-check timeout in the shared configuration repository was set below the service's startup time.",
        "A configuration cleanup removed the environment variable that pointed the service at its read replica.",
        "A rate-limit value was copied from the staging config into production, cutting allowed traffic by ninety percent.",
        "The deployment configuration still referenced the expired TLS certificate bundle after the rotation.",
        "A routing rule in the edge configuration sent all EU traffic to a single region.",
    ),
    "code": (
        "A refactor of the retry helper dropped the jitter calculation, so every client retried in lockstep.",
        "A new query in the orders module was missing its index-friendly predicate and scanned the whole table.",
        "An off-by-one error in the pagination code skipped the last page of results.",
        "The latest release removed a null check from the session middleware.",
        "A serializer change in the release wrote timestamps without a timezone offset.",
        "A dependency upgrade bundled into the release changed how JSON numbers were parsed.",
        "A race in the new cache warmer let two workers write the same key with different values.",
    ),
}
RESOLUTION = {
    "rolled back": (
        "We rolled the deploy back to the previous release, and error rates returned to baseline within minutes.",
        "On-call reverted to the last known good build through the deploy pipeline, which restored normal behaviour.",
        "The change was rolled back across all regions and the service recovered as soon as the rollback finished.",
    ),
    "not rolled back": (
        "A rollback was considered but rejected because the release also carried a schema migration; we shipped a forward fix instead.",
        "We did not roll back: the team patched the value in place and restarted the affected pods.",
        "Rolling back was discussed, but a hotfix was already in review, so we deployed the hotfix instead.",
    ),
}
IMPACT = {
    "customer": (
        "Customers could not complete checkout for about twenty minutes, and support received a spike of tickets.",
        "Some customers saw their order history pages fail to load until the cache was cleared.",
        "Shoppers on the mobile app were shown an empty basket after adding items, so many abandoned their orders.",
        "Customers in the EU received password-reset emails roughly an hour late.",
    ),
    "internal": (
        "Only the internal reporting dashboard lagged; nothing outside the company was affected.",
        "The nightly batch job finished three hours late; the delay stayed inside the data team's pipeline.",
        "Engineers could not deploy for forty minutes while the build cache rebuilt; production traffic was untouched.",
        "An internal alerting channel was flooded with duplicate pages, which the on-call rotation absorbed.",
    ),
}


def section(text, heading, path):
    match = re.search(r"^## " + re.escape(heading) + r"\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError(f"{path.name}: no '## {heading}' section")
    return match.group(1)


def classify(text, banks, path):
    labels = [label for label, sentences in banks.items() for sentence in sentences if sentence in text]
    if len(labels) != 1:
        raise AssertionError(f"{path.name}: expected exactly one known fact sentence, found {labels}; "
                             "update the bank in this test together with the fixture")
    return labels[0]


def config_lines(path, careless=False):
    """Non-comment lines; careless also reads commented-out ones as live."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if careless:
        return [line.lstrip("# ") for line in lines]
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def answer_cheap_retrieval(case_dir, careless=False):
    facts = {}
    for path in sorted((case_dir / "incidents").glob("INC-*.md")):
        text = path.read_text(encoding="utf-8")
        facts[path.stem] = (classify(section(text, "Root cause", path), CAUSE, path),
                            classify(section(text, "Resolution", path), RESOLUTION, path))
    return [k for k, v in sorted(facts.items()) if v == ("config", "rolled back")], sorted(facts)


def answer_local_small_task(case_dir, careless=False):
    deploy = case_dir / "deploy"
    active = re.fullmatch(r"profile = (\S+)", (deploy / "ACTIVE").read_text(encoding="utf-8").strip())
    ports = {}
    for path in sorted((deploy / "profiles").glob("*.env")):
        values = dict(line.split("=", 1) for line in config_lines(path))
        ports[str(path.relative_to(deploy))] = values["LISTEN_PORT"]
    return [ports[active.group(1)]], sorted(set(ports.values()))


def answer_worker_contract(case_dir, careless=False):
    missing, names = [], []
    for path in sorted((case_dir / "manifests").glob("*.yaml")):
        keys = {line.split(":", 1)[0].strip() for line in config_lines(path, careless) if ":" in line}
        name = path.stem
        names.append(name)
        if "owner" not in keys:
            missing.append(name)
    return missing, names


def answer_escalation(case_dir, careless=False):
    customer = []
    paths = sorted((case_dir / "postmortems").glob("PM-*.md"))
    for path in paths:
        if classify(section(path.read_text(encoding="utf-8"), "Impact", path), IMPACT, path) == "customer":
            customer.append(path.stem)
    return customer, [p.stem for p in paths]


def answer_quality_guard(case_dir, careless=False):
    over, names = [], []
    for path in sorted((case_dir / "services").glob("*.toml")):
        values = {}
        for line in config_lines(path, careless):
            match = re.fullmatch(r"(timeout_ms|retries) = (\d+)(?:\s.*)?", line.strip())
            if match and careless:
                values.setdefault(match.group(1), int(match.group(2)))  # the first one read wins
            elif match:
                if match.group(1) in values:
                    raise AssertionError(f"{path.name}: {match.group(1)} set twice")
                values[match.group(1)] = int(match.group(2))
        names.append(path.stem)
        if values["timeout_ms"] * (values["retries"] + 1) > 10000:
            over.append(path.stem)
    return over, names


DERIVATIONS = {
    "cheap-retrieval": answer_cheap_retrieval,
    "local-small-task": answer_local_small_task,
    "worker-contract": answer_worker_contract,
    "escalation-after-failed-check": answer_escalation,
    "quality-guard": answer_quality_guard,
}


class SuiteLayout(unittest.TestCase):
    """What `claude plugin eval` would refuse to load, or load as the wrong thing."""

    @classmethod
    def setUpClass(cls):
        cls.cases = load_suite()

    def test_every_case_has_an_offline_answer_check(self):
        self.assertEqual(sorted(self.cases), sorted(DERIVATIONS))

    def test_prompt_frontmatter_uses_documented_fields_and_limits(self):
        for name, case in self.cases.items():
            with self.subTest(case=name):
                meta = case["meta"]
                self.assertTrue(NAME_RE.fullmatch(name), name)
                self.assertLessEqual(set(meta), PROMPT_KEYS)
                self.assertIsInstance(meta["max_turns"], int)
                self.assertTrue(1 <= meta["max_turns"] <= 200)
                self.assertIsInstance(meta["timeout_seconds"], int)
                self.assertTrue(1 <= meta["timeout_seconds"] <= 3600)
                if "runs" in meta:
                    self.assertTrue(1 <= meta["runs"] <= 50)
                self.assertTrue(all(ENV_KEY_RE.fullmatch(k) for k in meta.get("env", {})))
                # Nothing beyond the read-only set, so no case needs --allow-tools
                # and no run needs the OS sandbox.
                self.assertLessEqual(set(meta["allowed_tools"]), READ_ONLY_TOOLS)
                self.assertTrue(case["body"].strip(), "the prompt body is what Claude receives")

    def test_case_yaml_names_the_plugin_and_read_only_fixtures(self):
        for name, case in self.cases.items():
            with self.subTest(case=name):
                data, case_dir = case["case"], case["dir"]
                self.assertEqual(data["schema_version"], "1.1")
                self.assertEqual(data["name"], name)
                self.assertLessEqual(set(data), CASE_KEYS)
                execution = data.get("execution", {})
                self.assertLessEqual(set(execution), EXECUTION_KEYS)
                # A record of strings; any key outside EVAL_* fails the run.
                for key, value in execution.get("env", {}).items():
                    self.assertTrue(ENV_KEY_RE.fullmatch(key), key)
                    self.assertIsInstance(value, str)
                for rel in data["plugins"]:
                    self.assertEqual((case_dir / rel).resolve(), ROOT)
                    self.assertTrue((case_dir / rel / ".claude-plugin" / "plugin.json").is_file())
                context = data["context"]
                self.assertLessEqual(set(context), CONTEXT_KEYS)
                # add_dirs only grants reads: the run starts in an empty
                # workspace and is never told where a granted directory is.
                self.assertNotIn("add_dirs", context)
                self.assertTrue(fixture_dirs(case_dir), "every case reads fixtures")

    def test_every_case_pins_a_parent_with_a_cheaper_tier_below_it(self):
        # Unpinned, a run takes the account's default model, so the suite
        # measured whatever that was (Sonnet 5.5 on 2026-10-10). A cheap parent
        # has no cheaper tier to delegate to. Escalation goes one tier above
        # cheap, to standard; under a standard parent that is the parent's own
        # price, which the policy keeps local, so that case needs a premium one.
        for name, case in self.cases.items():
            with self.subTest(case=name):
                self.assertIn(case["meta"].get("model"), ("sonnet", "opus"))
        self.assertEqual(self.cases["escalation-after-failed-check"]["meta"]["model"], "opus")

    def test_every_case_stages_its_fixtures_into_the_workspace(self):
        # Claude Code 2.1.296 turns add_dirs into Read/Glob/Grep allow rules
        # only, never --add-dir, and the run's cwd is empty, so a model given
        # only add_dirs Globs, finds nothing and gives up in both arms. The
        # documented way to hand a run its fixtures is a scaffold_script the
        # runner executes as `bash <script>` in the empty workspace (with
        # --scaffold) and the prompt naming the copied directory.
        for name, case in self.cases.items():
            with self.subTest(case=name):
                case_dir = case["dir"]
                script = case["case"]["context"].get("scaffold_script")
                self.assertEqual(script, SCAFFOLD, "one script name across the suite")
                path = case_dir / script
                self.assertTrue(path.is_file() and not path.is_symlink(), script)
                with tempfile.TemporaryDirectory() as tmp:
                    workspace, home = Path(tmp, "work"), Path(tmp, "home")
                    workspace.mkdir()
                    home.mkdir()
                    # The environment the runner gives a scaffold script.
                    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
                           "TMPDIR": tmp, "TERM": "dumb", "GIT_CONFIG_NOSYSTEM": "1"}
                    done = subprocess.run(["bash", str(path.resolve())], cwd=workspace, env=env,
                                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                          timeout=60)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    staged = sorted(p.name for p in workspace.iterdir())
                    self.assertEqual(staged, fixture_dirs(case_dir))
                    for rel in staged:
                        source, copy = case_dir / rel, workspace / rel
                        self.assertFalse(any(p.is_symlink() for p in source.rglob("*")), rel)
                        self.assertEqual(tree(copy), tree(source), rel)
                        # Read-only files, as the prompt says. Directories stay
                        # writable so the runner can delete the workspace.
                        files = [p for p in copy.rglob("*") if p.is_file()]
                        self.assertTrue(files, rel)
                        self.assertFalse(any(p.stat().st_mode & 0o222 for p in files), rel)
                        # The model finds a fixture only by the name the prompt gives it.
                        self.assertIn(f"`{rel}`", case["body"])

    def test_graders_use_documented_types_and_options(self):
        for name, case in self.cases.items():
            self.assertTrue(case["graders"], f"{name}: a case without a grader fails to load")
            for grader_name, (grader, body) in case["graders"].items():
                with self.subTest(case=name, grader=grader_name):
                    self.assertTrue(NAME_RE.fullmatch(grader_name))
                    kind = grader["type"]
                    self.assertIn(kind, TYPE_KEYS)
                    self.assertLessEqual(set(grader), GRADER_KEYS | TYPE_KEYS[kind])
                    if "arm" in grader:
                        self.assertIn(grader["arm"], ("with-only", "both"))
                    if "weight" in grader:
                        self.assertGreater(grader["weight"], 0)
                    if kind == "regex":
                        self.assertIsInstance(grader["pattern"], str)
                        self.assertIn(grader.get("target", "last_message"), TARGETS)
                    if kind == "tool_used":
                        low, high = grader.get("min", 1), grader.get("max")
                        self.assertTrue(isinstance(low, int) and low >= 0)
                        self.assertTrue(high is None or (isinstance(high, int) and high >= low))
                        # A grader on a tool the case cannot call measures nothing.
                        self.assertIn(grader["tool"], case["meta"]["allowed_tools"])
                    # Options live in frontmatter; a body would be read as rubric or pattern.
                    self.assertEqual(body.strip(), "")

    def test_regexes_stay_inside_the_subset_both_engines_agree_on(self):
        for name, case in self.cases.items():
            for grader_name, (grader, _) in case["graders"].items():
                pattern = grader.get("pattern") or grader.get("input_match")
                if not pattern:
                    continue
                with self.subTest(case=name, grader=grader_name):
                    re.compile(pattern)
                    for construct, why in FORBIDDEN:
                        self.assertIsNone(construct.search(pattern), why)
                    flags = str(grader.get("flags", ""))
                    self.assertLessEqual(set(flags), set(PY_FLAGS))
                    if re.search(r"(?<!\\)\$", pattern):
                        # Python's bare $ also matches before a final newline; JavaScript's does not.
                        self.assertIn("m", flags)

    def test_every_case_scores_correctness_in_both_arms(self):
        # Delta is with-arm minus without-arm over the graders scored in both. A
        # correctness grader in every case keeps it a quality comparison, and the
        # plugin-only indicators (agent names, contract lines) stay out of it.
        for name, case in self.cases.items():
            with self.subTest(case=name):
                grader, _ = case["graders"]["correct-answer"]
                self.assertTrue(scored_in_both_arms(grader))
                self.assertEqual(grader["type"], "regex")
                self.assertEqual(grader.get("target", "last_message"), "last_message")

    def test_no_grader_calls_a_judge_model(self):
        # The documented cost estimate counts agent runs only.
        for name, case in self.cases.items():
            for grader_name, (grader, _) in case["graders"].items():
                with self.subTest(case=name, grader=grader_name):
                    self.assertNotIn(grader["type"], JUDGE_TYPES)
        self.assertFalse(list(EVALS.rglob("mocks")))

    def test_run_results_stay_out_of_git(self):
        self.assertIn("results/", (EVALS / ".gitignore").read_text(encoding="utf-8").split())


class CorrectAnswers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_suite()

    def grader(self, case, name="correct-answer"):
        return self.cases[case]["graders"][name][0]

    def test_each_grader_accepts_the_answer_derived_from_its_fixture_and_nothing_near_it(self):
        for name, derive in DERIVATIONS.items():
            with self.subTest(case=name):
                answer, universe = derive(self.cases[name]["dir"])
                grader = self.grader(name)
                self.assertTrue(answer)
                self.assertTrue(regex_passes(grader, final_message(answer)))
                self.assertTrue(regex_passes(grader, "Done.\n\n**ANSWER:** " + ", ".join(answer) + ".\n"))
                self.assertTrue(regex_passes(grader, "ANSWER: " + ",".join(answer) + "  "))
                wrong = [item for item in universe if item not in answer]
                near = [sorted(answer + wrong[:1]), sorted(answer + wrong[-1:])]
                if len(answer) > 1:
                    near += [answer[:-1], answer[1:], list(reversed(answer))]
                for variant in near:
                    self.assertFalse(regex_passes(grader, final_message(variant)), variant)
                # The line has to stand alone; an answer buried in a sentence is not one.
                self.assertFalse(regex_passes(grader, "So the ANSWER: " + ", ".join(answer) + " holds."))

    def test_the_traps_in_the_fixtures_change_the_answer_and_are_graded_as_wrong(self):
        # Reading commented-out lines as live gives a different, wrong answer.
        for name in ("worker-contract", "quality-guard"):
            with self.subTest(case=name):
                case_dir = self.cases[name]["dir"]
                careful, _ = DERIVATIONS[name](case_dir)
                careless, _ = DERIVATIONS[name](case_dir, careless=True)
                self.assertNotEqual(careful, careless)
                self.assertFalse(regex_passes(self.grader(name), final_message(careless)))

    def test_the_staged_cheap_result_really_fails_its_named_check(self):
        case = self.cases["escalation-after-failed-check"]
        staged = re.search(r"```\n(.+)\n", case["body"]).group(1).split(", ")
        answer, _ = answer_escalation(case["dir"])
        index = (case["dir"] / "postmortems" / "INDEX.md").read_text(encoding="utf-8")
        count = int(re.search(r"^Customer-facing incidents this quarter: (\d+)$", index, re.MULTILINE).group(1))
        self.assertEqual(len(answer), count)
        self.assertLess(len(staged), count)
        self.assertLess(set(staged), set(answer))
        self.assertFalse(regex_passes(self.grader("escalation-after-failed-check"), final_message(staged)))


# Tier graders read the effective tier the way Claude Code resolves it: an
# explicit model wins, otherwise the named profile's own model applies.
CHEAP = [agent_input("leos-agent:leo-cheap"), agent_input("leo-cheap"), agent_input("leos-agent:leo-runner"),
         agent_input("general-purpose", "haiku"), agent_input("general-purpose", "claude-haiku-5-5"),
         agent_input("leos-agent:leo-cheap", "haiku"), agent_input("leos-agent:leo-standard", "haiku")]
STANDARD = [agent_input("leos-agent:leo-standard"), agent_input("leo-standard"),
            agent_input("leos-agent:leo-executor"), agent_input("general-purpose", "sonnet"),
            agent_input("general-purpose", "claude-sonnet-5-5")]
# Model-less generic dispatches inherit (or take the guard's default), and a
# premium profile under a standard parent is clamped by the guard: no explicit
# cheap or standard choice was made, so only the corrected form can name a tier.
OTHER = [agent_input("general-purpose"), agent_input("Explore"), agent_input("leos-agent:leo-premium"),
         agent_input("general-purpose", "opus"), agent_input("other-plugin:leo-cheap"),
         agent_input("other-plugin:leo-standard")]


class RoutingGraders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_suite()

    def grader(self, case, name):
        return self.cases[case]["graders"][name][0]

    def assert_matches(self, grader, positives, negatives):
        for args in positives:
            self.assertTrue(tool_used_passes(grader, [("Agent", args)]), args)
        for args in negatives:
            self.assertFalse(tool_used_passes(grader, [("Agent", args)]), args)

    def assert_same_verdict_through_the_guard(self, grader, samples):
        # The transcript may record the model's input or the guard's corrected
        # one; the grader must not care which.
        for args in samples:
            corrected = guard_view(args)
            self.assertEqual(tool_used_passes(grader, [("Agent", args)]),
                             tool_used_passes(grader, [("Agent", corrected)]), (args, corrected))

    def test_cheap_delegation_recognises_the_cheap_tier_in_any_form(self):
        grader = self.grader("cheap-retrieval", "cheap-delegation")
        self.assertEqual(grader["arm"], "with-only")  # the baseline has no leo-* agents
        self.assertNotEqual(guard_view(CHEAP[0]), CHEAP[0], "the guard should rewrite a model-less profile dispatch")
        self.assert_matches(grader, CHEAP, STANDARD + OTHER)
        self.assert_same_verdict_through_the_guard(grader, CHEAP + STANDARD + OTHER)
        self.assertFalse(tool_used_passes(grader, [("Read", {"file_path": "incidents/INC-1001.md"})]))
        self.assertTrue(tool_used_passes(grader, [("Glob", {"pattern": "*.md"}), ("Agent", CHEAP[0])]))

    def test_the_guard_does_not_make_a_default_dispatch_look_cheap(self):
        grader = self.grader("cheap-retrieval", "cheap-delegation")
        corrected = guard_view(agent_input("general-purpose"))
        self.assertIn("model", corrected)
        self.assertFalse(tool_used_passes(grader, [("Agent", corrected)]))

    def test_local_task_fails_on_any_dispatch_in_either_arm(self):
        grader = self.grader("local-small-task", "stays-local")
        self.assertEqual(grader["arm"], "both")
        self.assertTrue(scored_in_both_arms(grader))
        self.assertTrue(tool_used_passes(grader, [("Read", {"file_path": "deploy/ACTIVE"}), ("Glob", {"pattern": "*"})]))
        for args in CHEAP + STANDARD + OTHER:
            self.assertFalse(tool_used_passes(grader, [("Read", {}), ("Agent", args)]), args)

    def test_escalation_brief_must_open_with_the_cheap_marker(self):
        grader = self.grader("escalation-after-failed-check", "escalation-brief")
        opening = ("Escalation from cheap: the cheap worker returned 3 IDs but postmortems/INDEX.md "
                   "counts 4 customer-facing postmortems. Re-read every Impact section.")
        good = [opening, "**Escalation from cheap:** " + opening[22:], "# Escalation from cheap:\n" + opening[22:],
                "escalation from cheap: " + opening[22:]]
        bad = ["Re-read the postmortems. " + opening, "Escalation from standard: " + opening[22:],
               "Escalation: the cheap result failed its check.", "Please list customer-facing postmortems."]
        self.assert_matches(grader, [agent_input("leos-agent:leo-standard", prompt=p) for p in good],
                            [agent_input("leos-agent:leo-standard", prompt=p) for p in bad])
        for prompt in good:
            # The observer reads the same briefs as escalations from cheap.
            self.assertEqual(outcome.escalation_tier(prompt), "cheap", prompt)

    def test_escalation_goes_one_tier_up_and_never_back_to_cheap(self):
        up = self.grader("escalation-after-failed-check", "one-tier-up")
        self.assert_matches(up, STANDARD, CHEAP + OTHER)
        self.assert_same_verdict_through_the_guard(up, STANDARD + CHEAP)
        retry = self.grader("escalation-after-failed-check", "no-cheap-retry")
        self.assertEqual(retry["input_match"], self.grader("cheap-retrieval", "cheap-delegation")["input_match"])
        self.assertTrue(tool_used_passes(retry, [("Agent", STANDARD[0])]))
        for args in CHEAP:
            self.assertFalse(tool_used_passes(retry, [("Agent", STANDARD[0]), ("Agent", args)]), args)


class WorkerContractGrader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = load_suite()["worker-contract"]
        cls.grader = cls.case["graders"]["result-and-verified-lines"][0]

    def trace(self, reply=None, brief="Find manifests without an owner field."):
        """A session trace: one JSON message per line, as the runner hands it to a regex."""
        lines = [
            js_json({"type": "user", "message": {"role": "user", "content": self.case["body"]}}),
            js_json({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_01", "name": "Agent",
                 "input": agent_input("leos-agent:leo-cheap", prompt=brief)}]}}),
        ]
        if reply is not None:
            lines.append(js_json({"type": "user", "message": {"role": "user", "content": [
                {"tool_use_id": "toolu_01", "type": "tool_result", "content": [
                    {"type": "text", "text": reply},
                    {"type": "text", "text": "agentId: a1b2c3d4 (use it to resume this agent)"}]}]}}))
        lines.append(js_json({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "ANSWER: catalog, media, search"}]}}))
        return "\n".join(lines)

    def test_a_worker_reply_with_both_closing_lines_passes(self):
        replies = [
            "Manifests with no owner field: catalog, media, search.\n\nResult: done\nVerified: read all ten manifests",
            "- catalog\n- media\n- search\n\n**Result:** done\n**Verified:** grep -L '^owner:' manifests/*.yaml",
            "Only partly checked.\nResult: partial\n\nVerified: none",
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                self.assertTrue(regex_passes(self.grader, self.trace(reply)))
                self.assertIn(outcome.parse(reply)["outcome"], outcome.OUTCOMES)

    def test_the_contract_quoted_in_a_brief_or_profile_does_not_pass(self):
        template = ("End your reply with two lines: `Result: done|partial|blocked|escalate` and\n"
                    "`Verified: <the command or evidence you ran, or none>`.")
        profile = (ROOT / "agents" / "leo-cheap.md").read_text(encoding="utf-8").split("---", 2)[2]
        for brief in (template, profile, "Return Result: done\nVerified: <evidence>"):
            with self.subTest(brief=brief[:40]):
                self.assertFalse(regex_passes(self.grader, self.trace(None, brief=brief)))

    def test_a_reply_missing_or_emptying_a_line_fails(self):
        for reply in ("catalog, media, search\nResult: done", "catalog, media, search\nResult: done\nVerified:",
                      "catalog, media, search\nVerified: read them\nResult: done", "catalog, media, search"):
            with self.subTest(reply=reply):
                self.assertFalse(regex_passes(self.grader, self.trace(reply)))


def run_env(case):
    """The environment a case gives its run, as the installed runner merges it:
    prompt.md frontmatter replaces case.yaml's `execution:` field by field, so
    a frontmatter `env` would replace the case.yaml one whole."""
    if "env" in case["meta"]:
        return dict(case["meta"]["env"])
    return dict(case["case"].get("execution", {}).get("env", {}))


class RunEnvironment(unittest.TestCase):
    """Every run starts in a fresh home with no price catalog, where the
    plugin's SessionStart hook would start a background price refresh: network
    I/O in each of the suite's thirty runs. Only an allowlist of the shell and
    EVAL_* variables reach a run, so LEOS_AGENT_PRICE_REFRESH=off cannot."""

    @classmethod
    def setUpClass(cls):
        cls.cases = load_suite()

    def session_start(self, case_env):
        """emit_payload's SessionStart hook with the run's environment as its
        whole environment: no shell LEOS_AGENT_* variables, HOME and the Claude
        config in a fresh temporary home. Returns the refreshes it started."""
        with tempfile.TemporaryDirectory() as home:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"),
                   "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                   "LEOS_AGENT_LOCAL_PATH": os.path.join(home, ".leos-agent-local"),
                   "CLAUDE_PLUGIN_ROOT": str(ROOT), **case_env}
            event = {"session_id": "eval-run", "hook_event_name": "SessionStart", "source": "startup",
                     "model": "claude-sonnet-5-5", "cwd": home, "transcript_path": os.path.join(home, "t.jsonl")}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(pricing, "refresh_background") as refresh, \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(emit_payload.main([]), 0)
            self.assertTrue(out.getvalue().strip(), "the policy is still delivered")
            return refresh.call_count

    def test_no_run_starts_a_price_refresh(self):
        self.assertEqual(self.session_start({}), 1, "a fresh home without the opt-out refreshes")
        for name, case in self.cases.items():
            with self.subTest(case=name):
                env = run_env(case)
                self.assertTrue(all(ENV_KEY_RE.fullmatch(key) for key in env))
                self.assertEqual(self.session_start(env), 0)

    def test_the_prefixed_opt_out_means_what_the_plain_one_does(self):
        for env, refreshes in (({"LEOS_AGENT_PRICE_REFRESH": "off"}, 0), ({"EVAL_LEOS_AGENT_PRICE_REFRESH": "off"}, 0),
                               ({"EVAL_LEOS_AGENT_PRICE_REFRESH": "on"}, 1), ({"EVAL_LEOS_AGENT_PRICE_REFRESH": ""}, 1),
                               ({"EVAL_PRICE_REFRESH": "off"}, 1)):
            with self.subTest(env=env):
                self.assertEqual(self.session_start(env), refreshes)


if __name__ == "__main__":
    unittest.main()
