"""The structural gate refuses what AGENTS.md says it refuses.

The rules themselves live in scripts/check.py; these tests feed them inputs on
both sides of each line, then run the gate on a copy of the repository with
violations planted, the way a commit would meet it.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("check_gate_test", ROOT / "scripts" / "check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class TestPluginRootLint(unittest.TestCase):
    def test_a_path_built_from_the_claude_variable_is_refused(self):
        # Claude Code substitutes the braced form when it loads a body; the
        # other five harnesses read it verbatim and run /scripts/doctor.py.
        for line in ('python3 "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py" --json',
                     "Read $CLAUDE_PLUGIN_ROOT/rules/preferences.md first."):
            with self.subTest(line=line):
                self.assertTrue(check.plugin_root_violations(line + "\n"))

    def test_naming_the_variable_as_a_place_to_look_is_fine(self):
        text = ("Resolve the plugin root from `LEOS_AGENT_ROOT`, `$CLAUDE_PLUGIN_ROOT`,\n"
                "`PLUGIN_ROOT`, or the nearest ancestor containing `rules/preferences.md`.\n")
        self.assertEqual(check.plugin_root_violations(text), [])

    def test_every_script_invocation_goes_through_the_placeholder(self):
        self.assertEqual(check.plugin_root_violations('python3 "<plugin-root>/scripts/doctor.py" --json\n'), [])
        for line in ("python3 /opt/leos-agent/scripts/doctor.py", "   python3 scripts/doctor.py --json",
                     'python3 "$ROOT/scripts/doctor.py"'):
            with self.subTest(line=line):
                self.assertTrue(check.plugin_root_violations(line + "\n"))


class TestPortableFrontmatter(unittest.TestCase):
    def test_claude_only_keys_are_not_portable(self):
        text = ("---\nname: example\ndescription: An example.\nallowed-tools:\n  - Bash(gh pr view *)\n"
                "model: example-model\n---\n\nBody.\n")
        self.assertEqual(check.non_portable_keys(text), ["allowed-tools", "model"])

    def test_the_keys_portable_skills_use_pass(self):
        text = ('---\nname: example\ndisable-model-invocation: true\ndescription: >-\n  Folded: still one key.\n'
                'argument-hint: "[pr-number]"\n---\n\nBody with a line like\nmodel: not frontmatter\n')
        self.assertEqual(check.non_portable_keys(text), [])


class TestSkillTriggerLint(unittest.TestCase):
    def test_a_model_invocable_skill_needs_a_trigger_clause(self):
        self.assertTrue(check.skill_trigger_problems("Review a pull request.", True))
        self.assertEqual(check.skill_trigger_problems("Review a pull request. Use when asked to review a PR.", True), [])

    def test_a_model_invocable_description_has_a_byte_cap(self):
        long = "Use when asked. " + "x" * check.MODEL_INVOCABLE_DESCRIPTION_CAP
        self.assertTrue(check.skill_trigger_problems(long, True))

    def test_a_user_invoked_skill_carries_no_trigger(self):
        self.assertTrue(check.skill_trigger_problems("Write a handoff. Use when context runs low.", False))
        # The cap guards the always-loaded listing; a hidden skill may run longer.
        self.assertEqual(check.skill_trigger_problems("Write a handoff. " + "x" * 400, False), [])

    def test_folded_descriptions_are_read_whole(self):
        text = "---\nname: example\ndescription: >-\n  Load a handoff.\n  Use when one is named.\nargument-hint: x\n---\nBody\n"
        self.assertEqual(check.frontmatter_description(text), "Load a handoff. Use when one is named.")
        self.assertEqual(check.frontmatter_description("---\ndescription: Plain one.\n---\n"), "Plain one.")


def tree(case, files):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    for rel, text in files.items():
        path = Path(tmp.name) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return Path(tmp.name)


class TestPythonFloor(unittest.TestCase):
    def test_a_match_statement_fails_with_file_and_line(self):
        root = tree(self, {"scripts/ok.py": "x = 1\n",
                           "scripts/new.py": "x = 1\nmatch x:\n    case 1:\n        pass\n"})
        problems = check.python_floor_problems(root)
        self.assertEqual(len(problems), 1, problems)
        # The line is where the parser gave up, inside the match block.
        self.assertRegex(problems[0], r"^scripts/new\.py:[2-4]: not Python 3\.9 syntax")

    def test_the_real_scripts_parse(self):
        self.assertEqual(check.python_floor_problems(ROOT), [])


TOOL = """import argparse

def build_parser():
    parser = argparse.ArgumentParser(prog="tool.py")
    sub = parser.add_subparsers(dest="mode", required=True)
    go = sub.add_parser("go")
    go.add_argument("-R", required=True)
    go.add_argument("-n", type=int, required=True)
    go.add_argument("--commit", type=lambda v: v if len(v) == 40 else int("bad"))
    go.add_argument("--out")
    go.add_argument("--harness", choices=("claude", "codex"))
    go.add_argument("--since")
    go.add_argument("rest", nargs="*")
    return parser
"""


class TestDocumentedCommands(unittest.TestCase):
    def test_a_bad_flag_in_a_skill_fails_with_file_and_line(self):
        root = tree(self, {"scripts/tool.py": TOOL, "skills/x/SKILL.md":
                           '# X\n\n```sh\npython3 "<plugin-root>/scripts/tool.py" go -R OWNER/REPO -n N --bogus\n```\n'})
        problems = check.documented_command_problems(root)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("skills/x/SKILL.md:4: tool.py go"), problems)
        self.assertIn("unrecognized arguments: --bogus", problems[0])

    def test_placeholders_continuations_inline_code_and_optional_groups_parse(self):
        doc = ('Run `python3 "<plugin-root>/scripts/tool.py" go -R\nOWNER/REPO -n N` first.\n\n'
               '```sh\npython3 "<plugin-root>/scripts/tool.py" go -R OWNER/REPO -n N --commit SHA \\\n'
               '  --out "$SCRATCH/a.json" [--since PREV] --harness <harness> <paths\u2026> > "$SCRATCH/b"\n'
               'python3 "<plugin-root>/scripts/tool.py" go -R OWNER/REPO -n N ...   # a comment\n```\n')
        root = tree(self, {"scripts/tool.py": TOOL, "skills-claude/y/reference/r.md": doc})
        self.assertEqual(check.documented_command_problems(root), [])
        found = check.documented_commands(doc)
        self.assertEqual([(number, script) for number, script, _ in found], [(1, "tool"), (5, "tool"), (7, "tool")])
        self.assertEqual(check.command_argv(found[1][2]),
                         ["go", "-R", "o/r", "-n", "1", "--commit", "a" * 40, "--out", "/tmp/x/a.json",
                          "--harness", "claude", "x"])

    def test_a_script_without_build_parser_is_named(self):
        root = tree(self, {"scripts/bare.py": "import sys\n",
                           "skills/x/SKILL.md": '`python3 "<plugin-root>/scripts/bare.py" go`\n'})
        self.assertEqual(check.documented_command_problems(root),
                         ["skills/x/SKILL.md:1: scripts/bare.py has no build_parser(); add one"])

    def test_the_real_skills_parse(self):
        self.assertEqual(check.documented_command_problems(ROOT), [])


class TestEmDashes(unittest.TestCase):
    def test_an_em_dash_planted_in_a_skill_fails_with_file_and_line(self):
        root = tree(self, {"skills/x/SKILL.md": "fine\nthis \u2014 not\n", "tests/t.py": "s = '\u2014'\n"})
        self.assertEqual(check.em_dash_problems(root), ["skills/x/SKILL.md:2: em dash; rewrite the sentence"])

    def test_script_strings_fail_but_comments_and_named_detectors_do_not(self):
        root = tree(self, {"scripts/s.py": '"""Doc \u2015 string."""\n# a comment \u2014 here\nx = "ok"\n',
                           "scripts/outcome.py": '_SEP = r"[:\u2014]"\n'})
        self.assertEqual(check.em_dash_problems(root), ["scripts/s.py:1: string literal holds an em dash; rewrite it"])


class TestTheGateFailsOnPlantedViolations(unittest.TestCase):
    def test_check_py_refuses_a_non_portable_skill_and_a_claude_rooted_command(self):
        with tempfile.TemporaryDirectory(prefix="leo check gate ") as tmp:
            base = Path(tmp)
            copy = base / "repo"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", ".claude", "__pycache__", "node_modules", "tests"))
            doctor = copy / "skills" / "doctor" / "SKILL.md"
            doctor.write_text(doctor.read_text(encoding="utf-8").replace(
                "---\n", "---\nallowed-tools:\n  - Bash(gh pr view *)\n", 1), encoding="utf-8")
            handoff = copy / "skills" / "handoff" / "SKILL.md"
            handoff.write_text(handoff.read_text(encoding="utf-8")
                               + '\n```sh\npython3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" list\n```\n',
                               encoding="utf-8")
            env = {**os.environ, "HOME": str(base / "home"), "LEOS_AGENT_LOCAL_PATH": str(base / "data"),
                   "CLAUDE_CONFIG_DIR": str(base / "claude"), "CODEX_HOME": str(base / "codex"),
                   "HERMES_HOME": str(base / "hermes"), "PI_CODING_AGENT_DIR": str(base / "pi"),
                   "OPENCODE_CONFIG_DIR": str(base / "opencode"),
                   "OPENCODE_CONFIG": str(base / "opencode" / "opencode.jsonc"),
                   "XDG_CONFIG_HOME": str(base / "xdg"), "LEOS_AGENT_PRICE_REFRESH": "off",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            env.pop("LEOS_AGENT_ROOT", None)
            (base / "home").mkdir()
            result = subprocess.run([sys.executable, str(copy / "scripts" / "check.py")], cwd=str(copy),
                                    env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        failures = [line for line in result.stderr.splitlines() if line.startswith("FAIL ")]
        self.assertEqual(len(failures), 2, result.stderr)
        self.assertIn("skills/doctor/SKILL.md: non-portable frontmatter ['allowed-tools']", failures[0])
        self.assertIn("skills/handoff/SKILL.md: builds a path from CLAUDE_PLUGIN_ROOT", failures[1])



    def test_check_py_refuses_a_policy_edit_that_orphans_a_claude_drop(self):
        with tempfile.TemporaryDirectory(prefix="leo check drop ") as tmp:
            base = Path(tmp)
            copy = base / "repo"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", ".claude", "__pycache__", "node_modules", "tests"))
            policy = copy / "rules" / "preferences.md"
            text = policy.read_text(encoding="utf-8")
            self.assertIn("Do not copy history or repeat returned work.", text)
            policy.write_text(text.replace("Do not copy history or repeat returned work.",
                                           "Avoid copying history."), encoding="utf-8")
            env = {**os.environ, "HOME": str(base / "home"), "LEOS_AGENT_LOCAL_PATH": str(base / "data"),
                   "CLAUDE_CONFIG_DIR": str(base / "claude"), "CODEX_HOME": str(base / "codex"),
                   "HERMES_HOME": str(base / "hermes"), "PI_CODING_AGENT_DIR": str(base / "pi"),
                   "OPENCODE_CONFIG_DIR": str(base / "opencode"),
                   "OPENCODE_CONFIG": str(base / "opencode" / "opencode.jsonc"),
                   "XDG_CONFIG_HOME": str(base / "xdg"), "LEOS_AGENT_PRICE_REFRESH": "off",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            env.pop("LEOS_AGENT_ROOT", None)
            (base / "home").mkdir()
            result = subprocess.run([sys.executable, str(copy / "scripts" / "check.py")], cwd=str(copy),
                                    env=env, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CLAUDE_DROP", result.stderr)


class TestIdentifyingStringGate(unittest.TestCase):
    def tree(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for rel, text in files.items():
            path = Path(tmp.name) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return Path(tmp.name)

    def test_a_planted_denied_token_fails_without_echoing_it(self):
        word = "zzplantedword"
        root = self.tree({"notes/a.txt": "fine\n", "b.md": f"zz{word}x is another word\n"})
        problems = check.identifying_string_problems(root, deny={check.deny_digest(word)})
        self.assertEqual(problems, [])
        # A denied word inside a longer slug still matches.
        root = self.tree({"notes/a.txt": f"fine\nsee fix/{word.upper()}-1-foo here\n"})
        self.assertEqual(len(check.identifying_string_problems(root, deny={check.deny_digest(word)})), 1)
        root = self.tree({"notes/a.txt": f"fine\nsee {word.upper()} here\n"})
        problems = check.identifying_string_problems(root, deny={check.deny_digest(word)})
        self.assertEqual(len(problems), 1)
        self.assertIn("notes/a.txt:2", problems[0])
        self.assertNotIn(word, problems[0].lower())

    def test_a_clean_tree_passes(self):
        root = self.tree({"skills/x/SKILL.md": "Ask the user. Path /Users/<you>/work.\n", "README.md": "hi\n"})
        self.assertEqual(check.identifying_string_problems(root), [])

    def test_the_real_repo_passes(self):
        self.assertEqual(check.identifying_string_problems(ROOT), [])

    def test_a_placeholder_home_path_passes_in_a_skill(self):
        root = self.tree({"skills/x/SKILL.md": "cwd: /Users/<you>/workspace/x\n"})
        self.assertEqual(check.identifying_string_problems(root), [])

    def test_a_real_looking_home_path_fails_in_a_skill_but_not_in_tests(self):
        root = self.tree({"skills/x/SKILL.md": "cwd: /Users/someone-real/work\n",
                          "tests/t.py": "p = '/Users/me/secret'\nq = '/Users/someone/x'\n"})
        problems = check.identifying_string_problems(root)
        self.assertEqual(len(problems), 1)
        self.assertIn("skills/x/SKILL.md:1", problems[0])
        self.assertNotIn("someone-real", problems[0])

    def test_the_maintainer_name_fails_in_prompt_text_only(self):
        root = self.tree({"skills/x/SKILL.md": "Ask Leo first.\n", "agents/a.md": "Leo's call.\n",
                          "scripts/s.py": "# Leo wrote this\n"})
        problems = check.identifying_string_problems(root)
        self.assertEqual(sorted(p.split(":")[0] for p in problems), ["agents/a.md", "skills/x/SKILL.md"])

    def test_deny_hash_flag_prints_the_digest(self):
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / "check.py"), "--deny-hash", "Word"],
                             capture_output=True, text=True)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, check.deny_digest("word")))


if __name__ == "__main__":
    unittest.main()
