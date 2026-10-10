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


if __name__ == "__main__":
    unittest.main()
