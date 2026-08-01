"""OpenCode plugin packaging lint: adapters/opencode/agents.json,
adapters/opencode/plugin.js, and plugins/leo/package.json. Stdlib
unittest only — the plugin bridge is ESM/Node, so this file checks it
statically, in the style of tests/test_workflow_static.py.

Run: python3 -m unittest tests.test_opencode_plugin -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expected_version import EXPECTED_VERSION

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
MODEL_CONFIG = os.path.join(PAYLOAD, "config", "models.json")
AGENTS_JSON = os.path.join(PAYLOAD, "adapters", "opencode", "agents.json")
PLUGIN_JS = os.path.join(PAYLOAD, "adapters", "opencode", "plugin.js")
PACKAGE_JSON = os.path.join(PAYLOAD, "package.json")

READ_ONLY = {"expert", "explore", "investigator", "planner", "reviewer"}
DENIED_RM = {"rm -rf ~", "rm -rf ~/*", "rm -rf /", "rm -rf /*"}


def _load_config():
    with open(MODEL_CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def _load_agents():
    with open(AGENTS_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _read_plugin_js():
    with open(PLUGIN_JS, encoding="utf-8") as fh:
        return fh.read()


def _read_plugin_js_code():
    """plugin.js without its comment lines.

    The comments explain which cwd is wrong and why, so counting occurrences
    in the raw text finds the rationale rather than the code — the same reason
    TestReleaseWorkflowPublish strips '#' lines from the workflow.
    """
    return "\n".join(
        line for line in _read_plugin_js().splitlines() if not line.lstrip().startswith("//")
    )


class TestOpenCodeAgentsJson(unittest.TestCase):
    def test_exactly_six_roles_no_expert(self):
        agents = _load_agents()
        self.assertEqual(len(agents), 6)
        self.assertNotIn("expert", agents)

    def test_every_model_openrouter_prefixed_and_matches_config(self):
        config = _load_config()
        opencode = config["harnesses"]["opencode"]
        agents = _load_agents()
        for role, agent in agents.items():
            with self.subTest(role=role):
                tier = config["roles"][role]["tier"]
                self.assertTrue(agent["model"].startswith("openrouter/"))
                self.assertEqual(agent["model"], f"openrouter/{opencode[tier]['model']}")

    def test_read_only_roles_carry_edit_deny(self):
        agents = _load_agents()
        for role, agent in agents.items():
            with self.subTest(role=role):
                if role in READ_ONLY:
                    self.assertEqual(agent["permission"], {"edit": "deny"})
                else:
                    self.assertEqual(
                        set(agent["permission"].get("bash", {})), DENIED_RM
                    )
                    for value in agent["permission"]["bash"].values():
                        self.assertEqual(value, "deny")

    def test_every_prompt_non_empty(self):
        agents = _load_agents()
        for role, agent in agents.items():
            with self.subTest(role=role):
                self.assertTrue(agent["prompt"].strip())

    def test_mode_is_subagent(self):
        agents = _load_agents()
        for role, agent in agents.items():
            with self.subTest(role=role):
                self.assertEqual(agent["mode"], "subagent")


class TestOpenCodePluginJsStatic(unittest.TestCase):
    def test_declares_the_four_hooks(self):
        text = _read_plugin_js()
        for hook in ("async config(config)", "'experimental.chat.system.transform'", "'tool.execute.before'"):
            with self.subTest(hook=hook):
                self.assertIn(hook, text)

    def test_references_leo_policy_marker(self):
        text = _read_plugin_js()
        self.assertIn("<leo-policy>", text)
        self.assertIn("</leo-policy>", text)

    def test_no_frontmatter_parser_or_env_overrides(self):
        text = _read_plugin_js()
        self.assertNotIn("LEO_MODEL_", text)

    def test_references_bash_guard(self):
        text = _read_plugin_js()
        self.assertIn("bash-guard.py", text)

    def test_references_agents_json_not_agents_dir_parsing(self):
        text = _read_plugin_js()
        self.assertIn("agents.json", text)


class TestOpenCodeSessionDirectory(unittest.TestCase):
    """One OpenCode process hosts several project directories (its log shows
    one run= creating an instance per directory) and ESM caches the bridge once
    per process, so every directory-dependent value has to come from the
    PluginInput the plugin factory is handed. It previously came from
    tool.execute.before's input, which carries only {tool, sessionID, callID},
    so the guard judged every command against the server's cwd.
    """

    def test_directory_comes_from_plugin_input(self):
        text = _read_plugin_js_code()
        self.assertIn("ctx.directory || ctx.worktree", text)
        self.assertNotIn("input.directory", text)
        self.assertNotIn("input.worktree", text)
        self.assertNotIn("leoPlugin(_ctx)", text)

    def test_no_consumer_falls_back_to_the_server_cwd(self):
        # The single tolerated process.cwd() is the last-resort default when
        # PluginInput carries no directory at all.
        text = _read_plugin_js_code()
        self.assertEqual(text.count("process.cwd()"), 1)
        self.assertNotIn("cwd: process.cwd()", text)

    def test_per_directory_state_is_not_held_in_bare_module_variables(self):
        # A shared cache would pin whichever project started first and serve
        # its repo-scoped memory block to every other project in the process.
        text = _read_plugin_js_code()
        self.assertIn("policyCache = new Map()", text)
        self.assertIn("policyPathCache = new Map()", text)
        self.assertIn("opencode-policy-${directoryKey(directory)}.md", text)


class TestOpenCodeGuardFailureHandling(unittest.TestCase):
    """The guard allows what it cannot judge, matching Claude Code and Codex
    (PreToolUse blocks only on exit 2). Only Cursor fails closed, deliberately,
    via failClosed. What is not acceptable is being unbounded or silent.
    """

    def test_guard_spawn_is_bounded(self):
        text = _read_plugin_js_code()
        # One for the memory spawn, one for the guard spawn.
        self.assertEqual(text.count("setTimeout"), 2)
        self.assertIn("timed out after 10s", text)

    def test_every_unguarded_command_is_recorded(self):
        text = _read_plugin_js_code()
        self.assertNotIn("guardWarnedOnce", text)
        self.assertIn("opencode-guard.log", text)

    def test_doctor_surfaces_the_guard_log(self):
        with open(os.path.join(PAYLOAD, "scripts", "doctor.py"), encoding="utf-8") as fh:
            self.assertIn("opencode-guard.log", fh.read())


@unittest.skipUnless(shutil.which("node"), "node is required to drive the ESM bridge")
class TestOpenCodeGuardLive(unittest.TestCase):
    """Static lint cannot tell a threaded directory from an ignored one, so
    drive the real hook: the same command must be judged differently in two
    directories. `rm -rf .` is critical in $HOME and fine inside a project.
    """

    def _run_guard(self, directory, command):
        script = """
        const plugin = (await import(process.argv[1])).default;
        const hooks = await plugin({ directory: process.argv[2] });
        try {
          await hooks['tool.execute.before'](
            { tool: 'bash', sessionID: 's', callID: 'c' },
            { args: { command: process.argv[3] } },
          );
          console.log('ALLOW');
        } catch {
          console.log('BLOCK');
        }
        """
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, PLUGIN_JS, directory, command],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_guard_judges_against_the_session_directory(self):
        self.assertEqual(self._run_guard(os.path.expanduser("~"), "rm -rf ."), "BLOCK")
        self.assertEqual(self._run_guard(REPO, "rm -rf ."), "ALLOW")

    def test_unconditional_tripwire_still_fires(self):
        self.assertEqual(self._run_guard(REPO, "rm -rf ~"), "BLOCK")

    def test_unrunnable_guard_allows_and_leaves_a_breadcrumb(self):
        """With python3 off PATH the guard cannot run. The command proceeds —
        that is the same posture as Claude Code — but it must not do so
        silently, which is what the old one-shot warning latch caused.
        """
        with tempfile.TemporaryDirectory() as local:
            # node by absolute path, so an empty PATH strands only the
            # plugin's own `spawn('python3')` lookup.
            env = dict(os.environ, PATH="/nonexistent", LEOS_AGENT_LOCAL_PATH=local)
            result = subprocess.run(
                [
                    shutil.which("node"), "--input-type=module", "-e",
                    "const p=(await import(process.argv[1])).default;"
                    "const h=await p({directory:process.argv[2]});"
                    "try{await h['tool.execute.before']("
                    "{tool:'bash',sessionID:'s',callID:'c'},"
                    "{args:{command:'rm -rf ~'}});console.log('ALLOW')}"
                    "catch{console.log('BLOCK')}",
                    PLUGIN_JS, REPO,
                ],
                capture_output=True, text=True, timeout=60, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "ALLOW")
            with open(os.path.join(local, "opencode-guard.log"), encoding="utf-8") as fh:
                self.assertIn("guard did not run", fh.read())


class TestOpenCodePackageJson(unittest.TestCase):
    def test_name_and_version(self):
        with open(PACKAGE_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["name"], "leos-agent")
        self.assertEqual(data["version"], EXPECTED_VERSION)
        self.assertEqual(data["main"], "adapters/opencode/plugin.js")

    def test_files_exclude_claude_only_surfaces(self):
        with open(PACKAGE_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        for excluded in ("skills-claude/", "skills-claude"):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, data["files"])


class TestReleaseWorkflowPublish(unittest.TestCase):
    """The publish step is only exercised on a tag, so pin its shape here."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(REPO, ".github", "workflows", "release.yml")
        with open(path, encoding="utf-8") as fh:
            cls.workflow = fh.read()
        # The comments here explain why NPM_TOKEN is absent and why the two
        # publish steps are ordered as they are, so matching against raw text
        # would find the prose rather than the steps.
        cls.steps = "\n".join(
            line for line in cls.workflow.splitlines() if not line.lstrip().startswith("#")
        )

    def test_publish_path_is_relative(self):
        # A bare `plugins/leo` is read by npm as the GitHub shorthand
        # owner/repo, and the publish dies trying to clone it.
        self.assertIn("npm publish ./plugins/leo --access public", self.steps)
        self.assertNotIn("npm publish plugins/leo", self.steps)

    def test_trusted_publishing_not_token_auth(self):
        self.assertIn("id-token: write", self.steps)
        for token in ("NODE_AUTH_TOKEN", "NPM_TOKEN"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.steps)

    def test_npm_publish_precedes_github_release(self):
        # gh release create is not idempotent: if it ran first, a failed
        # publish could never be retried by re-running the workflow.
        self.assertLess(
            self.steps.index("npm publish ./plugins/leo"),
            self.steps.index("gh release create"),
        )


if __name__ == "__main__":
    unittest.main()
