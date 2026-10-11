import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import opencode_skills


def installer():
    spec = importlib.util.spec_from_file_location("leo_install_opencode_skills_test", ROOT / "scripts" / "leo-install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenCodeSkills(unittest.TestCase):
    """What the OpenCode 2 plugin registers is what leo-install copies for OpenCode 1."""

    def test_each_skill_matches_the_copy_opencode_1_would_get(self):
        module = installer()
        skills = {s["id"]: s for s in opencode_skills.skills()}
        self.assertEqual(sorted(skills), sorted(("leo-install",) + module.OPENCODE_SKILLS))
        for skill_id, info in skills.items():
            with self.subTest(skill=skill_id):
                src = Path(info["path"])
                self.assertTrue(src.is_file())
                self.assertEqual(src.parent.name, "install" if skill_id == "leo-install" else skill_id)
                copy = module.opencode_payload(src, ROOT, rename_install=skill_id == "leo-install")
                frontmatter, body = re.match(r"---\n(.*?)\n---\n?(.*)", copy, re.DOTALL).groups()
                self.assertEqual(info["content"], body)
                self.assertNotIn(module.PLUGIN_ROOT_TOKEN, info["content"])
                self.assertIn(f"name: {info['name']}", frontmatter)
                self.assertTrue(info["description"])
                user_invoked = "disable-model-invocation: true" in frontmatter
                self.assertEqual(info.get("autoinvoke", True), not user_invoked)
        self.assertEqual(skills["leo-install"]["name"], "leo-install")

    def test_the_script_prints_the_list_as_json(self):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "opencode_skills.py")], capture_output=True,
                             text=True, timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), opencode_skills.skills())


if __name__ == "__main__":
    unittest.main()
