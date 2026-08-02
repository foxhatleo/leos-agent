"""OpenCode plugin packaging lint: adapters/opencode/agents.json,
adapters/opencode/plugin.js, and plugins/leo/package.json. Stdlib
unittest only — the plugin bridge is ESM/Node, so this file checks it
statically, in the style of tests/test_workflow_static.py.

Run: python3 -m unittest tests.test_opencode_plugin -v
"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expected_version import EXPECTED_VERSION
from test_consistency import parse_frontmatter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
MODEL_CONFIG = os.path.join(PAYLOAD, "config", "models.json")
AGENTS_JSON = os.path.join(PAYLOAD, "adapters", "opencode", "agents.json")
PLUGIN_JS = os.path.join(PAYLOAD, "adapters", "opencode", "plugin.js")
PACKAGE_JSON = os.path.join(PAYLOAD, "package.json")

DENIED_RM = {"rm -rf ~", "rm -rf ~/*", "rm -rf /", "rm -rf /*"}


def _read_only_roles():
    """Derived from config, not restated here.

    A literal set in this file was one of three independent copies of the same
    fact (the renderer held another; the role prompts' tools: lines are the
    third and only enforceable one). test_consistency ties access to the
    prompts; this ties the generated roster to access.
    """
    return {r for r, s in _load_config()["roles"].items() if s["access"] == "read-only"}


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
    def test_roster_is_every_role_whose_tier_is_not_declared_absent(self):
        """Intent-derived, not a magic count.

        The old assertion was `len(agents) == 6`, which does fail if a role
        goes missing — but with the message "6 != 5", which reads as an
        off-by-one and invites the next person to edit the 6 to a 5.
        """
        config = _load_config()
        absent = set(config["harnesses"]["opencode"].get("absentTiers", ()))
        expected = {r for r, s in config["roles"].items() if s["tier"] not in absent}
        self.assertEqual(set(_load_agents()), expected)
        self.assertNotIn("expert", _load_agents())

    def test_a_tier_collapse_cannot_silently_drop_a_role(self):
        """The regression the old model-identity rule would have shipped.

        Point sonnet at the opus model and `implementer` used to vanish from
        the roster, because the rule dropped any role whose tier resolved to
        the opus model rather than any role whose tier was declared absent.
        """
        sys.path.insert(0, os.path.join(PAYLOAD, "scripts"))
        import render_adapters

        config = copy.deepcopy(_load_config())
        opencode = config["harnesses"]["opencode"]
        opencode["sonnet"]["model"] = opencode["opus"]["model"]
        agents = json.loads(render_adapters._opencode_agents(config))
        self.assertIn("implementer", agents)
        self.assertIn("executor", agents)
        self.assertNotIn("expert", agents, "fable is still the only declared-absent tier")

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
                if role in _read_only_roles():
                    self.assertEqual(agent["permission"].get("edit"), "deny")
                    self.assertEqual(set(agent["permission"].get("bash", {})), DENIED_RM)
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
        self.assertIn("policyInputsCache = null", text)
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

    def test_guard_child_early_exit_with_large_payload_fails_open_and_logs(self):
        with tempfile.TemporaryDirectory() as local, tempfile.TemporaryDirectory() as fake_bin:
            fake_python = os.path.join(fake_bin, "python3")
            with open(fake_python, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            os.chmod(fake_python, 0o755)
            command = "echo " + ("x" * 70_000)
            env = dict(os.environ, PATH=fake_bin, LEOS_AGENT_LOCAL_PATH=local)
            result = subprocess.run(
                [
                    shutil.which("node"), "--input-type=module", "-e",
                    "const p=(await import(process.argv[1])).default;"
                    "const h=await p({directory:process.argv[2]});"
                    "await h['tool.execute.before']({tool:'bash'},"
                    "{args:{command:process.argv[3]}});console.log('ALLOW')",
                    PLUGIN_JS, REPO, command,
                ],
                capture_output=True, text=True, timeout=60, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "ALLOW")
            with open(os.path.join(local, "opencode-guard.log"), encoding="utf-8") as fh:
                self.assertIn("guard did not run", fh.read())


@unittest.skipUnless(shutil.which("node"), "node is required to drive the ESM bridge")
class TestOpenCodeShadowSkillsTree(unittest.TestCase):
    """OpenCode requires a skill's frontmatter `name:` to equal its
    containing directory name and has no separate namespace, so plugin.js
    registers a generated shadow copy of skills/ with every skill renamed
    leo-<name> in place of the source tree (see plugin.js's own header
    comment for why). This drives the real config() hook rather than
    re-deriving the copy/rewrite/hash rules in Python — a reimplementation
    here could pass while the generator it is meant to guard silently
    drifted.

    Each test runs against its own throwaway copy of the plugin payload, so
    mutating a "source" SKILL.md to exercise cache invalidation never
    touches the repo.
    """

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="leo-opencode-skills-")
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.payload_copy = os.path.join(self.workdir, "leo")
        shutil.copytree(PAYLOAD, self.payload_copy)
        self.plugin_js = os.path.join(self.payload_copy, "adapters", "opencode", "plugin.js")
        self.local_state = os.path.join(self.workdir, "local-state")
        skills_dir = os.path.join(self.payload_copy, "skills")
        self.source_skill_names = sorted(
            name for name in os.listdir(skills_dir)
            if os.path.isfile(os.path.join(skills_dir, name, "SKILL.md"))
        )

    def _generate(self):
        """Runs the real config() hook and returns the shadow dir it registered
        in config.skills.paths — the same value OpenCode itself would read.
        """
        script = (
            "const plugin = (await import(process.argv[1])).default;"
            "const hooks = await plugin({ directory: process.argv[2] });"
            "const config = {};"
            "await hooks.config(config);"
            "console.log(config.skills.paths[0]);"
        )
        env = dict(os.environ, LEOS_AGENT_LOCAL_PATH=self.local_state)
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, self.plugin_js, self.workdir],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        shadow = result.stdout.strip()
        self.assertTrue(os.path.isdir(shadow), f"config() did not return a directory: {shadow!r}")
        return shadow

    def test_every_skill_appears_exactly_once_renamed_leo_prefixed(self):
        shadow = self._generate()
        registered = sorted(
            name for name in os.listdir(shadow)
            if os.path.isdir(os.path.join(shadow, name))
        )
        self.assertEqual(registered, [f"leo-{name}" for name in self.source_skill_names])

    def test_frontmatter_name_matches_the_shadow_directory_name(self):
        shadow = self._generate()
        for name in self.source_skill_names:
            with self.subTest(skill=name):
                shadow_name = f"leo-{name}"
                fm = parse_frontmatter(os.path.join(shadow, shadow_name, "SKILL.md"))
                self.assertEqual(fm.get("name"), shadow_name)

    def test_using_leo_references_travel_with_the_copy(self):
        shadow = self._generate()
        src_refs = os.path.join(self.payload_copy, "skills", "using-leo", "references")
        dest_refs = os.path.join(shadow, "leo-using-leo", "references")
        self.assertTrue(os.path.isdir(dest_refs), "references/ did not travel with the shadow copy")
        self.assertEqual(sorted(os.listdir(src_refs)), sorted(os.listdir(dest_refs)))
        for name in os.listdir(src_refs):
            with self.subTest(reference=name):
                with open(os.path.join(src_refs, name), encoding="utf-8") as fh:
                    src_text = fh.read()
                with open(os.path.join(dest_refs, name), encoding="utf-8") as fh:
                    dest_text = fh.read()
                self.assertEqual(src_text, dest_text)

    def test_regeneration_is_idempotent(self):
        first = self._generate()
        second = self._generate()
        self.assertEqual(first, second)
        with open(os.path.join(second, "leo-doctor", "SKILL.md"), encoding="utf-8") as fh:
            self.assertIn("name: leo-doctor", fh.read())

    def test_changed_source_file_produces_a_different_tree(self):
        first = self._generate()
        skill_md = os.path.join(self.payload_copy, "skills", "doctor", "SKILL.md")
        with open(skill_md, "a", encoding="utf-8") as fh:
            fh.write("\n<!-- leo-test-perturbation -->\n")

        second = self._generate()

        self.assertNotEqual(first, second)
        with open(os.path.join(second, "leo-doctor", "SKILL.md"), encoding="utf-8") as fh:
            self.assertIn("<!-- leo-test-perturbation -->", fh.read())

    def test_a_superseded_tree_survives_until_it_goes_stale(self):
        """A fresh tree is never swept, however superseded it looks.

        Two payloads can share one state root — the documented dev setup runs
        a working tree beside the installed package — and each session holds
        the path config() handed it. Deleting on hash mismatch alone would
        pull a live session's skills out from under it.
        """
        first = self._generate()
        skill_md = os.path.join(self.payload_copy, "skills", "doctor", "SKILL.md")
        with open(skill_md, "a", encoding="utf-8") as fh:
            fh.write("\n<!-- leo-test-perturbation -->\n")

        second = self._generate()
        self.assertNotEqual(first, second)
        self.assertTrue(
            os.path.isdir(first),
            "a freshly-used shadow tree was swept; a live session would have lost its skills",
        )

    def test_a_tree_untouched_past_the_grace_period_is_swept(self):
        first = self._generate()
        marker = os.path.join(first, ".leo-shadow-complete")
        self.assertTrue(os.path.isfile(marker), "completion marker missing")
        # Eight days: past the seven-day grace period in plugin.js.
        stale = time.time() - 8 * 24 * 60 * 60
        os.utime(marker, (stale, stale))
        os.utime(first, (stale, stale))

        skill_md = os.path.join(self.payload_copy, "skills", "doctor", "SKILL.md")
        with open(skill_md, "a", encoding="utf-8") as fh:
            fh.write("\n<!-- leo-test-perturbation -->\n")
        second = self._generate()

        self.assertNotEqual(first, second)
        self.assertFalse(
            os.path.isdir(first),
            "a tree untouched for longer than the grace period was not swept",
        )

    def test_reusing_a_tree_refreshes_its_marker(self):
        """The touch is what keeps a live tree young; without it the grace
        period would expire under a long-running session."""
        tree = self._generate()
        marker = os.path.join(tree, ".leo-shadow-complete")
        stale = time.time() - 8 * 24 * 60 * 60
        os.utime(marker, (stale, stale))

        self.assertEqual(self._generate(), tree)
        self.assertGreater(
            os.stat(marker).st_mtime,
            stale + 1,
            "re-using a shadow tree did not refresh its marker mtime",
        )

    def test_shadow_failure_registers_no_bare_skills_fallback_and_leaves_log(self):
        shadow = self._generate()
        shutil.rmtree(os.path.join(shadow, "leo-doctor"))
        with open(os.path.join(shadow, "leo-doctor"), "w", encoding="utf-8") as fh:
            fh.write("block copied skill directory")
        os.unlink(os.path.join(shadow, ".leo-shadow-complete"))
        script = (
            "const plugin=(await import(process.argv[1])).default;"
            "const hooks=await plugin({directory:process.argv[2]});const config={};"
            "await hooks.config(config);console.log(JSON.stringify(config.skills.paths));"
        )
        env = dict(os.environ, LEOS_AGENT_LOCAL_PATH=self.local_state)
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, self.plugin_js, self.workdir],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])
        with open(os.path.join(self.local_state, "opencode-skills.log"), encoding="utf-8") as fh:
            breadcrumb = fh.read()
        self.assertIn("shadow skills tree generation failed", breadcrumb)
        self.assertNotIn("falling back", breadcrumb.lower())


@unittest.skipUnless(shutil.which("node"), "node is required to drive the ESM bridge")
class TestOpenCodeRuntimePolicy(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="leo-opencode-runtime-")
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.payload_copy = os.path.join(self.workdir, "leo")
        shutil.copytree(PAYLOAD, self.payload_copy)
        self.plugin_js = os.path.join(self.payload_copy, "adapters", "opencode", "plugin.js")
        self.local_state = os.path.join(self.workdir, "local-state")

    def _run(self, script, *, env=None):
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script, self.plugin_js, self.workdir],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, LEOS_AGENT_LOCAL_PATH=self.local_state, **(env or {})),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_agents_are_namespaced_and_user_collision_wins(self):
        out = self._run(
            "const p=(await import(process.argv[1])).default;const h=await p({directory:process.argv[2]});"
            "const c={agent:{'leo-planner':{prompt:'user definition'}}};await h.config(c);"
            "console.log(JSON.stringify(c.agent))"
        )
        agents = json.loads(out)
        self.assertEqual(agents["leo-planner"], {"prompt": "user definition"})
        self.assertTrue(agents)
        self.assertTrue(all(name.startswith("leo-") for name in agents))
        self.assertNotIn("planner", agents)
        with open(os.path.join(self.local_state, "opencode-agents.log"), encoding="utf-8") as fh:
            self.assertIn("preserving user OpenCode agent definition for leo-planner", fh.read())

    def test_generated_policy_and_state_root_modes_are_private(self):
        out = self._run(
            "const p=(await import(process.argv[1])).default;const h=await p({directory:process.argv[2]});"
            "const c={};await h.config(c);console.log(c.instructions[0])"
        )
        policy_path = out.strip()
        self.assertEqual(os.stat(self.local_state).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(policy_path).st_mode & 0o777, 0o600)

    def test_config_rebuilds_memory_but_writes_only_when_digest_changes(self):
        fake_bin = os.path.join(self.workdir, "bin")
        os.mkdir(fake_bin)
        fake_python = os.path.join(fake_bin, "python3")
        with open(fake_python, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/sh\n"
                "cat \"$LEOS_AGENT_LOCAL_PATH/memory-current\"\n"
            )
        os.chmod(fake_python, 0o755)
        out = self._run(
            "const fs=await import('node:fs/promises');const p=(await import(process.argv[1])).default;"
            "await fs.mkdir(process.env.LEOS_AGENT_LOCAL_PATH,{recursive:true});"
            "const h=await p({directory:process.argv[2]});const a={},b={},c={};"
            "await fs.writeFile(process.env.LEOS_AGENT_LOCAL_PATH+'/memory-current','memory-one');await h.config(a);"
            "const one=await fs.readFile(a.instructions[0],'utf8');"
            "await fs.writeFile(process.env.LEOS_AGENT_LOCAL_PATH+'/memory-current','memory-two');await h.config(b);"
            "const two=await fs.readFile(b.instructions[0],'utf8');const before=(await fs.stat(b.instructions[0])).mtimeMs;"
            "await h.config(c);const after=(await fs.stat(c.instructions[0])).mtimeMs;"
            "console.log(JSON.stringify({same:a.instructions[0]===b.instructions[0],one,two,before,after}))",
            env={"PATH": fake_bin + os.pathsep + os.environ["PATH"]},
        )
        result = json.loads(out)
        self.assertTrue(result["same"])
        self.assertIn("memory-one", result["one"])
        self.assertIn("memory-two", result["two"])
        self.assertEqual(result["before"], result["after"])
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", result["two"])

    def test_transform_adds_the_real_policy_exactly_once(self):
        out = self._run(
            "const p=(await import(process.argv[1])).default;const h=await p({directory:process.argv[2]});"
            "const o={system:['base']};await h['experimental.chat.system.transform']({},o);"
            "await h['experimental.chat.system.transform']({},o);console.log(JSON.stringify(o.system))"
        )
        system = json.loads(out)
        policy = [item for item in system if isinstance(item, str) and "<leo-policy>" in item]
        self.assertEqual(len(policy), 1)
        self.assertIn("# Leo's global agent directives", policy[0])
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", policy[0])


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
        # The release helper receives the disposable staged directory with an
        # explicit relative path; it owns the npm invocation and retry rules.
        self.assertIn("--publish-npm ./npm-stage", self.steps)
        self.assertNotIn("--publish-npm npm-stage", self.steps)

    def test_trusted_publishing_not_token_auth(self):
        self.assertIn("id-token: write", self.steps)
        for token in ("NODE_AUTH_TOKEN", "NPM_TOKEN"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.steps)

    def test_npm_publish_precedes_github_release(self):
        # gh release create is not idempotent: if it ran first, a failed
        # publish could never be retried by re-running the workflow.
        self.assertLess(
            self.steps.index("--publish-npm ./npm-stage"),
            self.steps.index("--sync-github-release"),
        )


if __name__ == "__main__":
    unittest.main()
