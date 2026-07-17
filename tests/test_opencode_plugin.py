"""OpenCode plugin lint: package.json and .opencode/plugin/leo.js.
Stdlib unittest only (node --check runs as a subprocess, skipped if node
is not on PATH).

Run: python3 -m unittest tests.test_opencode_plugin -v
"""

import json
import os
import re
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_JSON = os.path.join(REPO, "package.json")
LEO_JS = os.path.join(REPO, ".opencode", "plugin", "leo.js")

OPENROUTER_MODELS = (
    "openrouter/z-ai/glm-5.2",
    "openrouter/minimax/minimax-m3",
    "openrouter/deepseek/deepseek-v4-pro",
)


def _load_package_json():
    with open(PACKAGE_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _leo_js_text():
    with open(LEO_JS, encoding="utf-8") as fh:
        return fh.read()


class TestNodeSyntaxCheck(unittest.TestCase):
    def test_node_check(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not found on PATH")

        result = subprocess.run(
            [node_bin, "--check", LEO_JS],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode, 0,
            f"node --check {LEO_JS} failed:\nstdout={result.stdout}\nstderr={result.stderr}",
        )


class TestPackageJson(unittest.TestCase):
    def test_core_fields(self):
        data = _load_package_json()
        self.assertEqual(data.get("name"), "leo")
        self.assertEqual(data.get("type"), "module")
        self.assertEqual(data.get("version"), "3.1.0")

    def test_main_exists(self):
        data = _load_package_json()
        main = data.get("main")
        self.assertTrue(main, "expected non-empty package.json 'main'")
        main_path = os.path.join(REPO, main)
        self.assertTrue(os.path.isfile(main_path), f"main entry {main_path} does not exist")


class TestLeoJsContent(unittest.TestCase):
    def test_openrouter_defaults(self):
        text = _leo_js_text()
        for model in OPENROUTER_MODELS:
            with self.subTest(model=model):
                self.assertIn(model, text)

    def test_leo_model_override_env_vars(self):
        text = _leo_js_text()
        self.assertIn("LEO_MODEL_OPUS", text)

    def test_bash_guard_tool_hook(self):
        text = _leo_js_text()
        self.assertIn("tool_name", text)
        self.assertIn("Bash", text)

    def test_exit_code_2_handling(self):
        text = _leo_js_text()
        self.assertTrue(
            re.search(r"===\s*2", text) or re.search(r"==\s*2", text),
            "expected exit-code-2 handling ('=== 2' or '== 2')",
        )

    def test_no_expert_registration(self):
        text = _leo_js_text()
        occurrences = [m.start() for m in re.finditer(r"expert", text, re.IGNORECASE)]
        if len(occurrences) <= 2:
            return
        for idx in occurrences:
            window = text[max(0, idx - 60):idx]
            with self.subTest(offset=idx):
                self.assertRegex(window, r"skip", "expert" + " reference not near a skip/exclusion context")

    def test_leo_policy_marker_dedupe(self):
        text = _leo_js_text()
        self.assertIn("<leo-policy>", text)
        self.assertTrue(
            re.search(r"already|dedup", text, re.IGNORECASE),
            "expected dedupe handling around the <leo-policy> marker",
        )


if __name__ == "__main__":
    unittest.main()
