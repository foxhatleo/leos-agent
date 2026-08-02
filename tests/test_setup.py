"""scripts/setup.py, including its `apply` action layer. Stdlib unittest only.

Every case redirects HOME and all four harness home variables into a temp
dir, and pops every harness-detection variable first — a case that forgot
either would read or write the developer's real ~/.claude.json,
~/.codex/config.toml, ~/.cursor/mcp.json or ~/.config/opencode/, which is
exactly what apply must never do. Where a harness's own CLI would otherwise
be shelled out to (`claude mcp add`, `codex mcp add`/`get`/`features list`),
subprocess.run is mocked rather than relying on those binaries being
installed on whatever machine runs this suite.

Run: python3 -m unittest tests.test_setup -v
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "plugins", "leo")
SETUP_PY = os.path.join(PAYLOAD, "scripts", "setup.py")

HARNESS_ENV_VARS = ("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "CURSOR_PLUGIN_ROOT",
                    "CURSOR_VERSION", "LEOS_AGENT_HARNESS")


def _load():
    spec = importlib.util.spec_from_file_location("leo_setup_probe", SETUP_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class SetupApplyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.env = {
            "HOME": os.path.join(base, "home"),
            "CLAUDE_CONFIG_DIR": os.path.join(base, "claude"),
            "CODEX_HOME": os.path.join(base, "codex"),
            "XDG_CONFIG_HOME": os.path.join(base, "xdg"),
            "HERMES_HOME": os.path.join(base, "hermes"),
            "LEOS_AGENT_LOCAL_PATH": os.path.join(base, "local"),
        }
        self._saved = {k: os.environ.get(k) for k in self.env}
        self._saved.update({k: os.environ.get(k) for k in HARNESS_ENV_VARS})
        for var in HARNESS_ENV_VARS:
            os.environ.pop(var, None)
        os.environ.update(self.env)
        os.makedirs(self.env["HOME"], exist_ok=True)
        self.setup = _load()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def mkgate(self, harness):
        gate = self.setup._harness_dir(harness)
        os.makedirs(gate, exist_ok=True)
        return gate

    def servers(self, harness):
        mcp = self.setup._mcp_config()
        return self.setup._core_servers(mcp, harness)

    def run_cli(self, *args, env_overrides=None):
        env = dict(os.environ)
        for var in HARNESS_ENV_VARS:
            env.pop(var, None)
        env.update(self.env)
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run([sys.executable, SETUP_PY, *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def capture_stdout(self, fn, *args, **kwargs):
        """Run an in-process cmd_* call with subprocess mocked (mocking only
        takes effect within this process, unlike run_cli's subprocess.run) and
        return (exit_code, stdout_text)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fn(*args, **kwargs)
        return code, buf.getvalue()

    def capture_output(self, fn, *args, **kwargs):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = fn(*args, **kwargs)
        return code, stdout.getvalue(), stderr.getvalue()


class TestExistingBehaviourUnaffected(SetupApplyCase):
    """The brief's PRESERVE EXACTLY section: report/enable/disable must be
    untouched by growing the apply layer beside them."""

    def test_bare_invocation_reports_and_changes_nothing(self):
        done = self.run_cli()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("leo setup", done.stdout)
        self.assertIn("hermes-memory", done.stdout)

    def test_json_flag_still_works(self):
        done = self.run_cli("--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads(done.stdout)
        self.assertIn("features", data)

    def test_enable_disable_roundtrip_unaffected(self):
        done = self.run_cli("enable", "hermes-memory")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("now on", done.stdout)
        done = self.run_cli("enable", "hermes-memory")
        self.assertIn("already on", done.stdout)
        done = self.run_cli("disable", "hermes-memory")
        self.assertIn("now off", done.stdout)

    def test_unknown_feature_still_exits_nonzero(self):
        done = self.run_cli("enable", "not-a-real-feature")
        self.assertNotEqual(done.returncode, 0)


class TestUnsupportedHarnessRefuses(SetupApplyCase):
    def test_no_signal_touches_nothing_and_exits_nonzero(self):
        done = self.run_cli("apply", "--dry-run")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("cannot determine a supported harness", done.stdout)
        for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "HERMES_HOME"):
            self.assertFalse(os.path.exists(self.env[var]),
                             f"apply must not create {var} when it refuses")

    def test_bogus_harness_argument_falls_back_to_unknown_rather_than_inventing(self):
        done = self.run_cli("apply", "--dry-run", "--harness", "bogus")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("cannot determine a supported harness", done.stdout)

    def test_real_apply_without_dry_run_also_refuses(self):
        done = self.run_cli("apply")
        self.assertNotEqual(done.returncode, 0)
        for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "HERMES_HOME"):
            self.assertFalse(os.path.exists(self.env[var]))


class TestMcpSchemaGate(SetupApplyCase):
    def test_invalid_catalogue_refuses_before_any_harness_action(self):
        invalid = {
            "servers": {"bad": {"label": "Bad", "transport": "stdio", "command": [], "auth": "none"}},
            "core": {"opencode": ["missing"]},
            "gating": {"opencode": {"off": ["missing"], "agents": {"nope": ["bad"]}}},
            "connectors": [],
        }
        with mock.patch.object(self.setup, "_mcp_config", return_value=invalid):
            code, out = self.capture_stdout(self.setup.cmd_apply, ["--harness", "opencode"])
        self.assertEqual(code, 1)
        self.assertIn("invalid MCP catalogue", out)
        self.assertFalse(os.path.exists(self.setup._harness_dir("opencode")))

    def test_reviewed_core_commands_are_exactly_pinned(self):
        servers = self.setup._mcp_config()["servers"]
        self.assertEqual(servers["context7"]["command"][-1], "@upstash/context7-mcp@3.2.5")
        self.assertEqual(servers["playwright"]["command"][-1], "@playwright/mcp@0.0.78")
        self.assertEqual(servers["chrome-devtools"]["command"][-1], "chrome-devtools-mcp@1.6.0")
        self.assertEqual(servers["duckduckgo"]["command"][-1], "duckduckgo-mcp-server==0.5.0")
        workflow = _read(os.path.join(PAYLOAD, "config", "MCP_PINS.md"))
        for spec in servers.values():
            self.assertIn(spec["exactVersion"], workflow)
        self.assertIn("Never use an unqualified package name, range, tag", workflow)

    def test_opencode_gating_targets_namespaced_leo_agents(self):
        agents = self.setup._mcp_config()["gating"]["opencode"]["agents"]
        self.assertIn("build", agents)
        self.assertTrue(
            all(name == "build" or name.startswith("leo-") for name in agents),
            "generated OpenCode gates must target the namespaced agents the plugin registers",
        )


class TestNeverCreatesAHarnessDirectory(SetupApplyCase):
    """memory.py:403-430's rule, mirrored: absence means not installed."""

    def test_absent_gate_is_reported_manual_and_nothing_is_created(self):
        cases = {
            "claude": self.setup._apply_claude,
            "cursor": self.setup._apply_cursor,
        }
        for harness, fn in cases.items():
            with self.subTest(harness=harness):
                gate = self.setup._harness_dir(harness)
                self.assertFalse(os.path.exists(gate))
                entries = fn(self.servers(harness), dry_run=False)
                self.assertFalse(os.path.exists(gate))
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["status"], "manual")
                self.assertIn("does not exist", entries[0]["detail"])

    def test_absent_codex_gate_never_shells_out(self):
        gate = self.setup._harness_dir("codex")
        self.assertFalse(os.path.exists(gate))
        with mock.patch.object(self.setup.subprocess, "run") as run:
            entries = self.setup._apply_codex(self.servers("codex"), dry_run=False)
        run.assert_not_called()
        self.assertFalse(os.path.exists(gate))
        self.assertEqual(entries[0]["status"], "manual")

    def test_absent_opencode_gate(self):
        gate = self.setup._harness_dir("opencode")
        self.assertFalse(os.path.exists(gate))
        entries = self.setup._apply_opencode(self.servers("opencode"), {}, dry_run=False)
        self.assertFalse(os.path.exists(gate))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "manual")


class TestDryRunCommandSets(SetupApplyCase):
    def test_claude_dry_run_produces_the_exact_command(self):
        self.mkgate("claude")
        entries = self.setup._apply_claude(self.servers("claude"), dry_run=True)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "would-install")
        self.assertEqual(entries[0]["command"], [
            "claude", "mcp", "add", "--scope", "user", "context7",
            "--", "npx", "-y", "@upstash/context7-mcp@3.2.5",
        ])

    def test_codex_dry_run_produces_the_exact_command_without_shelling_add(self):
        self.mkgate("codex")
        with mock.patch.object(self.setup.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout="[]", stderr="")):
            entries = self.setup._apply_codex(self.servers("codex"), dry_run=True)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "would-install")
        self.assertEqual(entries[0]["command"], [
            "codex", "mcp", "add", "context7", "--", "npx", "-y", "@upstash/context7-mcp@3.2.5",
        ])

    def test_cursor_dry_run_produces_the_exact_diff_and_no_command(self):
        self.mkgate("cursor")
        entries = self.setup._apply_cursor(self.servers("cursor"), dry_run=True)
        self.assertEqual(entries[0]["status"], "would-install")
        self.assertIsNone(entries[0]["command"])
        self.assertEqual(json.loads(entries[0]["diff"]), {
            "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp@3.2.5"], "env": {}},
        })

    def test_opencode_dry_run_lists_all_core_servers_and_gating(self):
        self.mkgate("opencode")
        mcp = self.setup._mcp_config()
        gating = mcp["gating"]["opencode"]
        entries = self.setup._apply_opencode(self.servers("opencode"), gating, dry_run=True)
        names = {e["server"] for e in entries if e["kind"] == "server"}
        self.assertEqual(names, {"context7", "playwright", "chrome-devtools", "duckduckgo"})
        self.assertTrue(all(e["status"] == "would-install" for e in entries if e["kind"] == "server"))
        gate_labels = {e["label"] for e in entries if e["kind"] == "gate"}
        self.assertIn('tools["context7*"] = false', gate_labels)
        self.assertIn('agent["build"].tools["playwright*"] = true', gate_labels)
        self.assertIn('agent["leo-implementer"].tools["playwright*"] = true', gate_labels)
        self.assertFalse(os.path.exists(os.path.join(self.setup._harness_dir("opencode"),
                                                      "opencode.jsonc")))

    def test_hermes_dry_run_never_touches_disk_and_prints_a_yaml_block(self):
        entries = self.setup._apply_hermes(self.servers("hermes"), dry_run=True)
        self.assertTrue(entries)
        self.assertTrue(all(e["status"] == "manual" for e in entries))
        self.assertFalse(os.path.exists(self.env["HERMES_HOME"]))
        block = entries[0]["diff"]
        self.assertIn("mcp_servers:", block)
        self.assertIn("context7:", block)
        self.assertIn("duckduckgo:", block)


class TestNeverTouchesOtherHarness(SetupApplyCase):
    def test_running_cursor_leaves_opencode_and_claude_config_untouched(self):
        self.mkgate("cursor")
        opencode_gate = self.mkgate("opencode")
        opencode_path = os.path.join(opencode_gate, "opencode.jsonc")
        with open(opencode_path, "w", encoding="utf-8") as fh:
            fh.write('{"mcp": {}}')
        self.mkgate("claude")
        claude_path = self.setup._claude_json_path()
        with open(claude_path, "w", encoding="utf-8") as fh:
            fh.write('{"mcpServers": {}}')

        before_oc, before_claude = _read(opencode_path), _read(claude_path)
        done = self.run_cli("apply", env_overrides={"LEOS_AGENT_HARNESS": "cursor"})
        self.assertEqual(done.returncode, 0, done.stderr)

        self.assertEqual(_read(opencode_path), before_oc)
        self.assertEqual(_read(claude_path), before_claude)
        cursor_path = os.path.join(self.setup._harness_dir("cursor"), "mcp.json")
        self.assertTrue(os.path.exists(cursor_path))
        self.assertIn("context7", _read(cursor_path))


class TestIdempotency(SetupApplyCase):
    def test_cursor_apply_twice_writes_nothing_the_second_time(self):
        self.mkgate("cursor")
        done1 = self.run_cli("apply", env_overrides={"LEOS_AGENT_HARNESS": "cursor"})
        self.assertEqual(done1.returncode, 0, done1.stderr)
        path = os.path.join(self.setup._harness_dir("cursor"), "mcp.json")
        before = (os.stat(path).st_mtime_ns, _read(path))

        done2 = self.run_cli("apply", env_overrides={"LEOS_AGENT_HARNESS": "cursor"})
        self.assertEqual(done2.returncode, 0, done2.stderr)
        self.assertIn("already-present", done2.stdout)
        self.assertNotIn("installed-now", done2.stdout)

        after = (os.stat(path).st_mtime_ns, _read(path))
        self.assertEqual(before, after)

    def test_opencode_apply_twice_writes_nothing_the_second_time(self):
        gate = self.mkgate("opencode")
        path = os.path.join(gate, "opencode.jsonc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{\n  // a comment\n  "mcp": {}\n}\n')

        done1 = self.run_cli("apply", env_overrides={"LEOS_AGENT_HARNESS": "opencode"})
        self.assertEqual(done1.returncode, 0, done1.stderr)
        before = (os.stat(path).st_mtime_ns, _read(path))

        done2 = self.run_cli("apply", env_overrides={"LEOS_AGENT_HARNESS": "opencode"})
        self.assertEqual(done2.returncode, 0, done2.stderr)
        self.assertNotIn("installed-now", done2.stdout)

        after = (os.stat(path).st_mtime_ns, _read(path))
        self.assertEqual(before, after)


class TestOpencodeJsoncRoundTrip(SetupApplyCase):
    def test_vendored_jsonc_editor_preserves_tabs_crlf_and_trailing_commas(self):
        original = "{\r\n\t// keep\r\n\t\"mcp\": {},\r\n}\r\n"
        before, error = self.setup._jsonc_parse(original)
        self.assertIsNone(error)
        after = {"mcp": {"context7": {"type": "local", "enabled": True}}}
        written, error = self.setup._jsonc_apply_missing(original, before, after)
        self.assertIsNone(error)
        self.assertIn("\r\n\t// keep\r\n", written)
        self.assertIn("\r\n\t\t\"context7\"", written)
        self.assertTrue(written.endswith("\r\n"))
        parsed, error = self.setup._jsonc_parse(written)
        self.assertIsNone(error)
        self.assertIn("context7", parsed["mcp"])

    def test_non_object_mcp_is_unknown_and_never_reported_installed(self):
        gate = self.mkgate("opencode")
        path = os.path.join(gate, "opencode.jsonc")
        original = '{\n  // preserve me\n  "mcp": false\n}\n'
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)

        code, out = self.capture_stdout(
            self.setup.cmd_apply, ["--harness", "opencode"]
        )

        self.assertEqual(code, 1)
        self.assertIn("mcp must be an object", out)
        self.assertNotIn("installed-now", out)
        self.assertEqual(_read(path), original)

    def test_non_object_agent_tools_refuses_instead_of_claiming_a_gate(self):
        gate = self.mkgate("opencode")
        path = os.path.join(gate, "opencode.jsonc")
        original = '{"agent": {"leo-implementer": {"tools": false}}}\n'
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)

        code, out = self.capture_stdout(
            self.setup.cmd_apply, ["--harness", "opencode"]
        )

        self.assertEqual(code, 1)
        self.assertIn("agent.leo-implementer.tools must be an object", out)
        self.assertEqual(_read(path), original)

    def test_comments_and_unrelated_keys_and_user_servers_do_not_block_the_write(self):
        gate = self.mkgate("opencode")
        path = os.path.join(gate, "opencode.jsonc")
        original = (
            '{\n'
            '  "$schema": "https://opencode.ai/config.json",\n'
            '  "plugin": ["leos-agent"], // installed via marketplace\n'
            '  "mcp": {\n'
            '    // a user-owned server, untouched by apply\n'
            '    "myOwnServer": {\n'
            '      "type": "local",\n'
            '      "command": ["echo", "http://example.com//not-a-comment"],\n'
            '      "enabled": true\n'
            '    }\n'
            '  }\n'
            '}\n'
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)

        done = self.run_cli("apply", env_overrides={"LEOS_AGENT_HARNESS": "opencode"})
        self.assertEqual(done.returncode, 0, done.stderr)

        written = _read(path)
        self.assertIn("// installed via marketplace", written)
        self.assertIn("// a user-owned server, untouched by apply", written)
        data, error = self.setup._jsonc_parse(written)
        self.assertIsNone(error)
        self.assertEqual(data["$schema"], "https://opencode.ai/config.json")
        self.assertEqual(data["plugin"], ["leos-agent"])
        self.assertEqual(data["mcp"]["myOwnServer"]["command"],
                         ["echo", "http://example.com//not-a-comment"])
        self.assertIn("context7", data["mcp"])
        self.assertIn("duckduckgo", data["mcp"])
        self.assertEqual(data["tools"]["context7*"], False)
        self.assertEqual(data["agent"]["build"]["tools"]["context7*"], True)

        # JSONC edits are surgical: comments and the original trailing-comma
        # style survive the write, rather than being recovered only from a
        # backup after a wholesale json.dumps rewrite.
        backup = path + ".leo-backup"
        self.assertTrue(os.path.exists(backup))
        self.assertEqual(_read(backup), original)
        self.assertNotIn("does not preserve", done.stdout)

    def test_a_url_looking_like_a_comment_inside_a_string_is_not_stripped(self):
        gate = self.mkgate("opencode")
        path = os.path.join(gate, "opencode.jsonc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"mcp": {"x": {"command": ["echo", "http://example.com/a"]}}}')
        parsed, error = self.setup._jsonc_parse(_read(path))
        self.assertIsNone(error)
        self.assertEqual(parsed["mcp"]["x"]["command"], ["echo", "http://example.com/a"])


class TestHermesNeverWrites(SetupApplyCase):
    def test_real_apply_emits_manual_instructions_and_writes_nothing(self):
        done = self.run_cli("apply", env_overrides={"LEOS_AGENT_HARNESS": "hermes"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("manual-steps", done.stdout)
        self.assertIn("mcp_servers:", done.stdout)
        self.assertFalse(os.path.exists(self.env["HERMES_HOME"]))


class TestClaudeInstallDetectionAndWrite(SetupApplyCase):
    def test_already_present_is_read_from_the_json_file_not_shelled_out(self):
        self.mkgate("claude")
        path = self.setup._claude_json_path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"context7": {"type": "stdio"}}}, fh)
        with mock.patch.object(self.setup.subprocess, "run") as run:
            entries = self.setup._apply_claude(self.servers("claude"), dry_run=False)
        run.assert_not_called()
        self.assertEqual(entries[0]["status"], "already-present")

    def test_real_apply_shells_out_and_records_success(self):
        self.mkgate("claude")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            entries = self.setup._apply_claude(self.servers("claude"), dry_run=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:6], ["claude", "mcp", "add", "--scope", "user", "context7"])
        self.assertEqual(entries[0]["status"], "installed")

    def test_a_failed_add_is_reported_failed_not_silently_dropped(self):
        """`failed`, not `manual` — the command ran and refused, which a
        caller chaining on `&&` has to be able to see."""
        self.mkgate("claude")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            entries = self.setup._apply_claude(self.servers("claude"), dry_run=False)
        self.assertEqual(entries[0]["status"], "failed")
        self.assertIn("boom", entries[0]["detail"])

    def test_a_missing_required_binary_is_reported_failed(self):
        self.mkgate("claude")
        with mock.patch.object(self.setup.subprocess, "run", side_effect=FileNotFoundError):
            entries = self.setup._apply_claude(self.servers("claude"), dry_run=False)
        self.assertEqual(entries[0]["status"], "failed")


class TestNeedsAuthReporting(SetupApplyCase):
    def test_shell_install_reports_needs_auth_when_the_server_requires_it(self):
        spec = {"label": "Fake", "command": ["fake-cmd"], "auth": "oauth", "authNote": "sign in first"}

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            entry = self.setup._shell_install("fake", spec, ["fake-cmd"])
        self.assertEqual(entry["status"], "needs-auth")
        self.assertEqual(entry["detail"], "sign in first")


class TestCodexToggleReport(SetupApplyCase):
    def test_computer_use_reads_from_features_list(self):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["codex", "features"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="computer_use    stable    true\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            toggles = self.setup._codex_toggles()
        by_name = {t["name"]: t for t in toggles}
        self.assertEqual(by_name["computer_use"]["status"], "already-on")

    def test_web_search_defaults_to_cached_and_offers_live_without_flipping_it(self):
        self.mkgate("codex")
        with mock.patch.object(self.setup.subprocess, "run", side_effect=FileNotFoundError):
            toggles = self.setup._codex_toggles()
        by_name = {t["name"]: t for t in toggles}
        self.assertEqual(by_name["web_search"]["status"], "offer")
        self.assertIn("live", by_name["web_search"]["detail"])
        # Never actually written — apply only ever reads this file for the toggle report.
        self.assertFalse(os.path.exists(os.path.join(self.setup._harness_dir("codex"), "config.toml")))

    def test_web_search_live_is_reported_already_live(self):
        gate = self.mkgate("codex")
        with open(os.path.join(gate, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('web_search = "live"\n')
        with mock.patch.object(self.setup.subprocess, "run", side_effect=FileNotFoundError):
            toggles = self.setup._codex_toggles()
        by_name = {t["name"]: t for t in toggles}
        self.assertEqual(by_name["web_search"]["status"], "already-live")

    def test_malformed_web_search_is_unknown_without_a_tomllib_claim(self):
        gate = self.mkgate("codex")
        with open(os.path.join(gate, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write("web_search = nope\n")
        with mock.patch.object(self.setup.subprocess, "run", side_effect=FileNotFoundError):
            toggles = self.setup._codex_toggles()
        web_search = {t["name"]: t for t in toggles}["web_search"]
        self.assertEqual(web_search["status"], "unknown")
        self.assertNotIn("tomllib", web_search["detail"])


class TestClaudeChromeToggle(SetupApplyCase):
    def test_never_reports_enabled_always_manual(self):
        toggles = self.setup._claude_toggles()
        self.assertEqual(len(toggles), 1)
        self.assertEqual(toggles[0]["status"], "manual")
        self.assertIn("/chrome", toggles[0]["detail"])


class TestSymlinkedConfigIsFollowed(SetupApplyCase):
    """A dotfiles-managed config is a symlink into the repo that owns it.

    Writing the link path replaces the link with a regular file and orphans
    the real one — the user's dotfiles silently stop tracking their config.
    """

    def test_opencode_write_follows_the_link_instead_of_replacing_it(self):
        gate = self.mkgate("opencode")
        real_dir = os.path.join(self.tmp.name, "dotfiles")
        os.makedirs(real_dir, exist_ok=True)
        real = os.path.join(real_dir, "opencode.jsonc")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write('{\n  "theme": "mine"\n}\n')
        link = os.path.join(gate, "opencode.jsonc")
        os.symlink(real, link)

        mcp = self.setup._mcp_config()
        gating = (mcp.get("gating") or {}).get("opencode") or {}
        self.setup._apply_opencode(self.servers("opencode"), gating, False)

        self.assertTrue(os.path.islink(link), "the symlink was replaced by a regular file")
        written = json.loads(_read(real))
        self.assertEqual(written.get("theme"), "mine", "the user's own key was lost")
        self.assertIn("context7", written.get("mcp", {}))
        self.assertTrue(
            os.path.isfile(real + ".leo-backup"),
            "the backup went next to the link rather than the real file",
        )


class TestConfigModeIsPreserved(SetupApplyCase):
    """A harness config can hold an API key in an `env` block, so 0600 is a
    legitimate mode for it. _atomic_text defaults to 0644, which would widen
    permissions on the one file most likely to hold a secret."""

    def test_a_restricted_config_stays_restricted(self):
        gate = self.mkgate("opencode")
        path = os.path.join(gate, "opencode.jsonc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{\n  "theme": "mine"\n}\n')
        os.chmod(path, 0o600)

        mcp = self.setup._mcp_config()
        gating = (mcp.get("gating") or {}).get("opencode") or {}
        self.setup._apply_opencode(self.servers("opencode"), gating, False)

        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertIn("context7", json.loads(_read(path)).get("mcp", {}))


class TestApplyExitCode(SetupApplyCase):
    def test_an_install_command_that_ran_and_failed_exits_non_zero(self):
        """Distinct from `manual`: Hermes and Claude-in-Chrome reach that
        having correctly done nothing, and 0 is right for them."""
        self.mkgate("claude")
        with open(self.setup._claude_json_path(), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="nope")
        with mock.patch.object(self.setup.subprocess, "run", return_value=failed):
            code, _, _ = self.capture_output(self.setup.cmd_apply, ["--harness", "claude"])
        self.assertEqual(code, 1)

    def test_a_missing_required_binary_exits_nonzero(self):
        self.mkgate("claude")
        with open(self.setup._claude_json_path(), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        with mock.patch.object(self.setup.subprocess, "run", side_effect=FileNotFoundError):
            code, _, _ = self.capture_output(self.setup.cmd_apply, ["--harness", "claude"])
        self.assertEqual(code, 1)

    def test_a_refused_write_exits_non_zero(self):
        gate = self.mkgate("opencode")
        with open(os.path.join(gate, "opencode.jsonc"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        code, _, _ = self.capture_output(self.setup.cmd_apply, ["--harness", "opencode"])
        self.assertEqual(code, 1)

    def test_a_clean_apply_exits_zero(self):
        gate = self.mkgate("opencode")
        with open(os.path.join(gate, "opencode.jsonc"), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        code, _, _ = self.capture_output(self.setup.cmd_apply, ["--harness", "opencode"])
        self.assertEqual(code, 0)


class TestRequiredRunnerPreflight(SetupApplyCase):
    def test_cursor_refuses_without_writing_when_npx_is_missing(self):
        gate = self.mkgate("cursor")
        path = os.path.join(gate, "mcp.json")
        with mock.patch.dict(os.environ, {"PATH": ""}):
            code, out, _ = self.capture_output(
                self.setup.cmd_apply, ["--harness", "cursor"]
            )
        self.assertEqual(code, 1)
        self.assertIn("required executable 'npx' is not available on PATH", out)
        self.assertFalse(os.path.exists(path))

    def test_opencode_refuses_atomically_when_core_runners_are_missing(self):
        gate = self.mkgate("opencode")
        path = os.path.join(gate, "opencode.jsonc")
        with mock.patch.dict(os.environ, {"PATH": ""}):
            code, out, _ = self.capture_output(
                self.setup.cmd_apply, ["--harness", "opencode"]
            )
        self.assertEqual(code, 1)
        self.assertIn("required executable 'npx' is not available on PATH", out)
        self.assertIn("required executable 'uvx' is not available on PATH", out)
        self.assertFalse(os.path.exists(path))


# ---------------------------------------------------------------------------
# connectors / connect: the vendor MCP menu, opt-in and never auto-offered
# ---------------------------------------------------------------------------

NO_LIST = subprocess.CompletedProcess([], 1, stdout="", stderr="")


class TestConnectorsReport(SetupApplyCase):
    """`connectors`/`connectors --json` are read-only over `mcp.connectors`."""

    def test_json_shape_and_url_match_under_a_different_server_name(self):
        gate = self.mkgate("claude")
        path = self.setup._claude_json_path()
        catalogue = self.setup._connector_catalogue()
        linear_url = catalogue["linear"]["url"]
        # Registered locally, but under a name that is not the connector key —
        # exactly the case the URL-primary match exists for.
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"my-own-linear": {"type": "http", "url": linear_url}}}, fh)
        before = (_read(path), os.stat(gate).st_mtime_ns)

        with mock.patch.object(self.setup.subprocess, "run", return_value=NO_LIST):
            code, out = self.capture_stdout(
                self.setup.cmd_connectors, ["--json", "--harness", "claude"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["harness"], "claude")
        self.assertTrue(data["supported"])

        by_key = {c["key"]: c for c in data["connectors"]}
        self.assertEqual(set(by_key), set(catalogue))
        for key in ("key", "label", "url", "transport", "auth", "authNote",
                    "installed", "needsUrl"):
            self.assertIn(key, by_key["linear"])
        self.assertTrue(by_key["linear"]["installed"],
                         "URL match under a different server name must count as installed")
        self.assertFalse(by_key["sentry"]["installed"])
        self.assertTrue(by_key["snowflake"]["needsUrl"])
        self.assertEqual(by_key["snowflake"]["url"], "")
        self.assertFalse(by_key["sentry"]["needsUrl"])

        self.assertEqual((_read(path), os.stat(gate).st_mtime_ns), before,
                         "connectors --json must never write, even to detect installs")
        self.assertFalse(os.path.exists(path + ".leo-backup"))

    def test_claude_mcp_list_output_alone_is_enough_to_mark_installed(self):
        """The real-world case this whole layer exists for: a claude.ai
        connector (Gmail, Drive, Granola, Vercel...) is registered against the
        account and never written into ~/.claude.json at all — only
        `claude mcp list` can see it."""
        self.mkgate("claude")
        catalogue = self.setup._connector_catalogue()
        granola_url = catalogue["granola"]["url"]

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["claude", "mcp", "list"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"claude.ai Granola: {granola_url} - ✔ Connected\n",
                    stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            code, out = self.capture_stdout(
                self.setup.cmd_connectors, ["--json", "--harness", "claude"])
        self.assertEqual(code, 0)
        by_key = {c["key"]: c for c in json.loads(out)["connectors"]}
        self.assertTrue(by_key["granola"]["installed"])
        self.assertFalse(by_key["vercel"]["installed"])

    def test_unsupported_harness_exits_zero_and_reports_nothing_installable(self):
        with mock.patch.object(self.setup.subprocess, "run", return_value=NO_LIST):
            code, out = self.capture_stdout(
                self.setup.cmd_connectors, ["--json", "--harness", "bogus"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertFalse(data["supported"])
        self.assertEqual(data["connectors"], [])

        code, out = self.capture_stdout(self.setup.cmd_connectors, ["--harness", "bogus"])
        self.assertEqual(code, 0)
        self.assertIn("cannot determine a supported harness", out)

    def test_absent_gate_reports_nothing_installed_without_creating_it(self):
        gate = self.setup._harness_dir("claude")
        self.assertFalse(os.path.exists(gate))
        with mock.patch.object(self.setup.subprocess, "run", return_value=NO_LIST) as run:
            code, out = self.capture_stdout(
                self.setup.cmd_connectors, ["--json", "--harness", "claude"])
        run.assert_not_called()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(gate))
        data = json.loads(out)
        self.assertTrue(data["supported"])
        self.assertTrue(all(not c["installed"] for c in data["connectors"]))


class TestConnectRefusals(SetupApplyCase):
    def test_snowflake_without_url_refuses_and_writes_nothing(self):
        self.mkgate("claude")
        path = self.setup._claude_json_path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"mcpServers": {}}')
        before = _read(path)
        with mock.patch.object(self.setup.subprocess, "run") as run:
            code, _, _ = self.capture_output(
                self.setup.cmd_connect, ["snowflake", "--harness", "claude"]
            )
        run.assert_not_called()
        self.assertNotEqual(code, 0)
        self.assertEqual(_read(path), before)
        self.assertFalse(os.path.exists(path + ".leo-backup"))

    def test_unknown_connector_key_refuses(self):
        self.mkgate("claude")
        with mock.patch.object(self.setup.subprocess, "run") as run:
            code, _, _ = self.capture_output(
                self.setup.cmd_connect, ["not-a-real-connector", "--harness", "claude"]
            )
        run.assert_not_called()
        self.assertNotEqual(code, 0)

    def test_url_flag_rejected_for_more_than_one_key(self):
        self.mkgate("claude")
        with mock.patch.object(self.setup.subprocess, "run") as run:
            code, _, _ = self.capture_output(
                self.setup.cmd_connect,
                ["linear", "sentry", "--url", "https://example.com/mcp", "--harness", "claude"],
            )
        run.assert_not_called()
        self.assertNotEqual(code, 0)

    def test_unsupported_harness_refuses_like_apply(self):
        code, out = self.capture_stdout(
            self.setup.cmd_connect, ["linear", "--harness", "bogus"])
        self.assertNotEqual(code, 0)
        self.assertIn("cannot determine a supported harness", out)


class TestConnectClaude(SetupApplyCase):
    def test_install_then_idempotent_then_needs_auth(self):
        self.mkgate("claude")
        path = self.setup._claude_json_path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {}}, fh)

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["claude", "mcp", "list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[:3] == ["claude", "mcp", "add"]:
                # What the real binary does: persists to ~/.claude.json itself.
                data = json.loads(_read(path))
                data.setdefault("mcpServers", {})[cmd[-2]] = {"type": "http", "url": cmd[-1]}
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                return subprocess.CompletedProcess(cmd, 0, stdout="Added HTTP MCP server", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        spec = dict(self.setup._connector_catalogue()["linear"])
        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run) as run:
            entries1 = self.setup._connect_claude([("linear", spec)])
        self.assertEqual(entries1[0]["status"], "needs-auth")
        self.assertEqual(entries1[0]["detail"], spec["authNote"])
        self.assertEqual(entries1[0]["command"][:5],
                         ["claude", "mcp", "add", "--transport", "http"])
        self.assertIn("--scope", entries1[0]["command"])
        self.assertIn("user", entries1[0]["command"])
        # `claude mcp list` (presence check) then `claude mcp add` (the install).
        self.assertEqual(run.call_count, 2)

        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            entries2 = self.setup._connect_claude([("linear", spec)])
        self.assertEqual(entries2[0]["status"], "already-present")

    def test_absent_gate_is_reported_manual_and_never_shells_out(self):
        gate = self.setup._harness_dir("claude")
        spec = dict(self.setup._connector_catalogue()["linear"])
        with mock.patch.object(self.setup.subprocess, "run") as run:
            entries = self.setup._connect_claude([("linear", spec)])
        run.assert_not_called()
        self.assertFalse(os.path.exists(gate))
        self.assertEqual(entries[0]["status"], "manual")

    def test_a_failed_add_is_reported_failed(self):
        self.mkgate("claude")
        with open(self.setup._claude_json_path(), "w", encoding="utf-8") as fh:
            fh.write('{"mcpServers": {}}')

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["claude", "mcp", "list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        spec = dict(self.setup._connector_catalogue()["linear"])
        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            entries = self.setup._connect_claude([("linear", spec)])
        self.assertEqual(entries[0]["status"], "failed")
        self.assertIn("boom", entries[0]["detail"])


class TestConnectCodex(SetupApplyCase):
    def test_install_then_idempotent_then_needs_auth(self):
        self.mkgate("codex")
        registered = []

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["codex", "mcp", "list"]:
                data = [{"name": n, "transport": {"type": "streamable_http", "url": u}}
                        for n, u in registered]
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")
            if cmd[:3] == ["codex", "mcp", "add"]:
                registered.append((cmd[3], cmd[-1]))
                return subprocess.CompletedProcess(cmd, 0, stdout="Added global MCP server", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        spec = dict(self.setup._connector_catalogue()["linear"])
        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            entries1 = self.setup._connect_codex([("linear", spec)])
            self.assertEqual(entries1[0]["status"], "needs-auth")
            self.assertEqual(entries1[0]["command"], ["codex", "mcp", "add", "linear",
                                                       "--url", spec["url"]])
            entries2 = self.setup._connect_codex([("linear", spec)])
            self.assertEqual(entries2[0]["status"], "already-present")

    def test_add_that_blocks_on_the_oauth_callback_still_reports_needs_auth(self):
        """codex mcp add writes the entry before it starts waiting on the
        browser callback, with no flag to skip the wait (confirmed against a
        real codex-cli 0.145.0 install) — setup must never block on it."""
        self.mkgate("codex")
        spec = dict(self.setup._connector_catalogue()["linear"])
        written = False  # flips only once the (timed-out) `add` has "landed"

        def fake_run(cmd, **kwargs):
            nonlocal written
            if cmd[:3] == ["codex", "mcp", "add"]:
                written = True
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
            if cmd[:3] == ["codex", "mcp", "list"]:
                data = [{"name": "linear", "transport": {"type": "streamable_http",
                                                          "url": spec["url"]}}] if written else []
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            entries = self.setup._connect_codex([("linear", spec)])
        self.assertEqual(entries[0]["status"], "needs-auth")

    def test_timeout_with_nothing_written_is_reported_manual_not_needs_auth(self):
        self.mkgate("codex")
        spec = dict(self.setup._connector_catalogue()["linear"])

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["codex", "mcp", "add"]:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            entries = self.setup._connect_codex([("linear", spec)])
        self.assertEqual(entries[0]["status"], "manual")

    def test_absent_gate_never_shells_out(self):
        gate = self.setup._harness_dir("codex")
        spec = dict(self.setup._connector_catalogue()["linear"])
        with mock.patch.object(self.setup.subprocess, "run") as run:
            entries = self.setup._connect_codex([("linear", spec)])
        run.assert_not_called()
        self.assertFalse(os.path.exists(gate))
        self.assertEqual(entries[0]["status"], "manual")


class TestConnectCursor(SetupApplyCase):
    def test_install_writes_url_shape_then_is_idempotent(self):
        self.mkgate("cursor")
        spec = dict(self.setup._connector_catalogue()["linear"])
        entries1 = self.setup._connect_cursor([("linear", spec)])
        self.assertEqual(entries1[0]["status"], "needs-auth")
        path = os.path.join(self.setup._harness_dir("cursor"), "mcp.json")
        written = json.loads(_read(path))
        self.assertEqual(written["mcpServers"]["linear"], {"url": spec["url"]})

        before = (os.stat(path).st_mtime_ns, _read(path))
        entries2 = self.setup._connect_cursor([("linear", spec)])
        self.assertEqual(entries2[0]["status"], "already-present")
        self.assertEqual((os.stat(path).st_mtime_ns, _read(path)), before)

    def test_absent_gate_never_creates_it(self):
        gate = self.setup._harness_dir("cursor")
        spec = dict(self.setup._connector_catalogue()["linear"])
        entries = self.setup._connect_cursor([("linear", spec)])
        self.assertFalse(os.path.exists(gate))
        self.assertEqual(entries[0]["status"], "manual")

    def test_installed_under_a_different_name_is_recognised(self):
        gate = self.mkgate("cursor")
        spec = dict(self.setup._connector_catalogue()["linear"])
        path = os.path.join(gate, "mcp.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"my-linear": {"url": spec["url"]}}}, fh)
        entries = self.setup._connect_cursor([("linear", spec)])
        self.assertEqual(entries[0]["status"], "already-present")
        self.assertNotIn("linear", json.loads(_read(path))["mcpServers"])


class TestConnectOpencode(SetupApplyCase):
    def test_install_writes_remote_shape_then_is_idempotent(self):
        self.mkgate("opencode")
        spec = dict(self.setup._connector_catalogue()["linear"])
        entries1 = self.setup._connect_opencode([("linear", spec)])
        self.assertEqual(entries1[0]["status"], "needs-auth")
        path = os.path.join(self.setup._harness_dir("opencode"), "opencode.jsonc")
        written = json.loads(_read(path))
        self.assertEqual(written["mcp"]["linear"],
                         {"type": "remote", "url": spec["url"], "enabled": True})

        before = (os.stat(path).st_mtime_ns, _read(path))
        entries2 = self.setup._connect_opencode([("linear", spec)])
        self.assertEqual(entries2[0]["status"], "already-present")
        self.assertEqual((os.stat(path).st_mtime_ns, _read(path)), before)

    def test_absent_gate_never_creates_it(self):
        gate = self.setup._harness_dir("opencode")
        spec = dict(self.setup._connector_catalogue()["linear"])
        entries = self.setup._connect_opencode([("linear", spec)])
        self.assertFalse(os.path.exists(gate))
        self.assertEqual(entries[0]["status"], "manual")


class TestConnectHermes(SetupApplyCase):
    def test_dynamic_registration_uses_add_then_login_without_writing_yaml(self):
        spec = dict(self.setup._connector_catalogue()["linear"])
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(self.setup.subprocess, "run", return_value=ok) as run:
            entries = self.setup._connect_hermes([("linear", spec)])
        self.assertEqual(entries[0]["status"], "needs-auth")
        self.assertFalse(os.path.exists(self.env["HERMES_HOME"]))
        self.assertEqual(run.call_args_list[0].args[0],
                         ["hermes", "mcp", "add", "linear", "--url", spec["url"], "--auth", "oauth"])
        self.assertEqual(run.call_args_list[1].args[0], ["hermes", "mcp", "login", "linear"])

    def test_known_empty_registry_reaches_dynamic_registration(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd == ["hermes", "mcp", "list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="No MCP servers configured\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(self.setup.subprocess, "run", side_effect=fake_run):
            code, out = self.capture_stdout(
                self.setup.cmd_connect, ["linear", "--harness", "hermes"]
            )

        self.assertEqual(code, 0)
        self.assertIn("needs-auth", out)
        self.assertIn(["hermes", "mcp", "add", "linear", "--url",
                       self.setup._connector_catalogue()["linear"]["url"],
                       "--auth", "oauth"], calls)
        self.assertIn(["hermes", "mcp", "login", "linear"], calls)


class TestConnectMultipleKeys(SetupApplyCase):
    def test_batch_connect_installs_every_key_and_url_override_applies_to_the_lone_key(self):
        self.mkgate("cursor")
        code, _, _ = self.capture_output(
            self.setup.cmd_connect, ["linear", "sentry", "--harness", "cursor"]
        )
        self.assertEqual(code, 0)
        path = os.path.join(self.setup._harness_dir("cursor"), "mcp.json")
        written = json.loads(_read(path))["mcpServers"]
        self.assertIn("linear", written)
        self.assertIn("sentry", written)

    def test_manual_snowflake_url_override_is_reported_without_writing(self):
        self.mkgate("cursor")
        custom = "https://my-org.snowflakecomputing.com/api/v2/databases/db/schemas/s/mcp-servers/n"
        code, _, _ = self.capture_output(
            self.setup.cmd_connect, ["snowflake", "--url", custom, "--harness", "cursor"]
        )
        self.assertEqual(code, 0)
        path = os.path.join(self.setup._harness_dir("cursor"), "mcp.json")
        self.assertFalse(os.path.exists(path))


class TestOldPythonFloor(unittest.TestCase):
    """Every shipped script must import cleanly on MIN_PYTHON.

    These are invoked as bare `python3 .../<script>.py` from skill docs and
    harness hooks, so they run on whatever `python3` resolves to — on stock
    macOS that is still 3.9. A module-scope import of a newer stdlib module
    takes the whole script down before it prints anything, including surfaces
    that have nothing to do with the new code (setup.py's report/enable/
    disable predate its apply layer and were collateral damage exactly once).

    Guards the class, not the instance: any module newer than the floor is
    caught, in any script, not just the one that got it wrong first.
    """

    MIN_PYTHON = (3, 9)
    # Stdlib modules newer than the floor. Entries at or below MIN_PYTHON
    # would be inert, so the map carries only ones that can actually fire.
    TOO_NEW = {
        "tomllib": (3, 11),
        "tomli_w": (3, 11),
        "asyncio.taskgroups": (3, 11),
    }

    def _scripts(self):
        root = os.path.join(PAYLOAD, "scripts")
        for name in sorted(os.listdir(root)):
            if name.endswith(".py"):
                yield os.path.join(root, name)
        hooks = os.path.join(PAYLOAD, "hooks")
        for name in sorted(os.listdir(hooks)):
            if name.endswith(".py"):
                yield os.path.join(hooks, name)

    def test_no_module_scope_import_is_newer_than_the_floor(self):
        import ast

        for path in self._scripts():
            tree = ast.parse(_read(path), filename=path)
            for node in tree.body:  # module scope only; lazy imports are fine
                roots = []
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split(".")[0]]
                for root in roots:
                    since = self.TOO_NEW.get(root)
                    if since and since > self.MIN_PYTHON:
                        self.fail(
                            f"{os.path.basename(path)} imports {root!r} at module scope; "
                            f"it needs Python {since[0]}.{since[1]} but these scripts must "
                            f"run on {self.MIN_PYTHON[0]}.{self.MIN_PYTHON[1]}. Import it "
                            "lazily inside the function that needs it and degrade when "
                            "it is unavailable."
                        )

    def test_every_script_parses_as_floor_syntax(self):
        """Catches 3.10+ *syntax* — `match`, runtime `X | Y` annotations.

        Deliberately not a py_compile against the system interpreter: that
        approach can only run where a floor-aged python happens to exist, so
        it skips on CI (3.12) — the one place a regression has to be caught.
        ast.parse's feature_version enforces the floor on any interpreter.
        """
        import ast

        for path in self._scripts():
            with self.subTest(script=os.path.basename(path)):
                try:
                    ast.parse(_read(path), filename=path, feature_version=self.MIN_PYTHON)
                except SyntaxError as exc:
                    self.fail(
                        f"{os.path.basename(path)} is not valid Python "
                        f"{self.MIN_PYTHON[0]}.{self.MIN_PYTHON[1]} syntax "
                        f"(line {exc.lineno}): {exc.msg}"
                    )


class TestEndpointMatching(SetupApplyCase):
    """The URL map exists to catch a connector registered under a name of the
    user's choosing. An exact string compare defeats that on a trailing slash
    or a capitalized host — and the cost is not a cosmetic miss: connect goes
    on to register the same endpoint a second time."""

    def test_normalization(self):
        norm = self.setup._normalize_endpoint
        canonical = norm("https://mcp.linear.app/mcp")
        for variant in (
            "https://mcp.linear.app/mcp/",
            "https://MCP.Linear.app/mcp",
            "  https://mcp.linear.app/mcp  ",
            "HTTPS://mcp.linear.app/mcp/",
        ):
            with self.subTest(variant=variant):
                self.assertEqual(norm(variant), canonical)
        self.assertNotEqual(norm("https://mcp.linear.app/other"), canonical)
        self.assertEqual(norm(""), "")
        self.assertEqual(norm(None), "")

    def test_a_variant_url_under_another_name_is_already_present(self):
        gate = self.mkgate("opencode")
        path = os.path.join(gate, "opencode.jsonc")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"mcp": {"my-linear": {
                "type": "remote", "url": "https://MCP.Linear.app/mcp/", "enabled": True,
            }}}, fh)

        spec = dict(self.setup._connector_catalogue()["linear"])
        entries = self.setup._connect_opencode([("linear", spec)])

        self.assertEqual(entries[0]["status"], "already-present")
        self.assertEqual(
            len(json.loads(_read(path))["mcp"]), 1,
            "connect registered a second entry for an endpoint already present",
        )

    def test_the_report_and_connect_agree(self):
        """A report saying `installed` while connect writes anyway is how one
        endpoint ends up registered twice."""
        gate = self.mkgate("opencode")
        with open(os.path.join(gate, "opencode.jsonc"), "w", encoding="utf-8") as fh:
            json.dump({"mcp": {"whatever": {
                "type": "remote", "url": "https://mcp.linear.app/mcp/", "enabled": True,
            }}}, fh)
        names, by_url, _ = self.setup._REGISTERED["opencode"]()
        connector = self.setup._connector_catalogue()["linear"]
        self.assertTrue(self.setup._connector_installed(connector, names, by_url))
        self.assertTrue(
            self.setup._already_registered("linear", connector["url"], names, by_url)
        )


class TestDetectionAvailability(SetupApplyCase):
    """"I looked and found nothing" and "I could not look" must not render the
    same. The second offering all eleven connectors would have the user
    re-register what they already have."""

    def test_a_failed_live_read_is_disclosed_not_reported_as_empty(self):
        self.mkgate("claude")
        with open(self.setup._claude_json_path(), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        with mock.patch.object(self.setup.subprocess, "run", side_effect=FileNotFoundError):
            code, out = self.capture_stdout(
                self.setup.cmd_connectors, ["--harness", "claude"]
            )
        self.assertEqual(code, 0)
        self.assertIn("could not read this harness's registered servers", out)

    def test_json_carries_detection_available_false(self):
        self.mkgate("claude")
        with open(self.setup._claude_json_path(), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        with mock.patch.object(self.setup.subprocess, "run", side_effect=FileNotFoundError):
            code, out = self.capture_stdout(
                self.setup.cmd_connectors, ["--json", "--harness", "claude"]
            )
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["detectionAvailable"])

    def test_a_successful_read_reports_detection_available(self):
        gate = self.mkgate("opencode")
        with open(os.path.join(gate, "opencode.jsonc"), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        code, out = self.capture_stdout(
            self.setup.cmd_connectors, ["--json", "--harness", "opencode"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["detectionAvailable"])

    def test_failed_hermes_live_detection_is_unknown(self):
        names, by_url, available = self.setup._REGISTERED["hermes"]()
        self.assertFalse(available, "a missing or failed Hermes CLI must remain unknown")


class TestClaudeListParsing(SetupApplyCase):
    """The `claude mcp list` format is not a contract. Drift that still parses
    lands a mangled key in the URL map, nothing matches it, and an installed
    connector reads as available — silent, and worse than useless."""

    def test_trailing_decoration_does_not_corrupt_the_url(self):
        parse = self.setup._parse_claude_mcp_list_line
        for line in (
            "claude.ai Granola: https://mcp.granola.ai/mcp - ✔ Connected",
            "claude.ai Granola: https://mcp.granola.ai/mcp - ✔ Connected - 12 tools",
            "claude.ai Granola: https://mcp.granola.ai/mcp (HTTP) - ✔ Connected",
        ):
            with self.subTest(line=line):
                parsed = parse(line)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed[1], "https://mcp.granola.ai/mcp")

    def test_headers_and_blanks_are_skipped(self):
        parse = self.setup._parse_claude_mcp_list_line
        for line in ("", "   ", "Checking MCP server health…"):
            self.assertIsNone(parse(line))

    def test_output_that_parses_to_nothing_is_treated_as_degraded(self):
        self.mkgate("claude")
        with open(self.setup._claude_json_path(), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        garbage = subprocess.CompletedProcess([], 0, stdout="!!! unexpected format !!!\n", stderr="")
        with mock.patch.object(self.setup.subprocess, "run", return_value=garbage):
            _, _, available = self.setup._claude_registered(self.setup._harness_dir("claude"))
        self.assertFalse(available, "unparseable output was treated as an authoritative empty list")


if __name__ == "__main__":
    unittest.main()
