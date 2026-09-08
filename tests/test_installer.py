"""Behavioral tests for the cross-harness installer."""

import importlib.util
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


# A fake Path.home() must not be bypassed by the invoking user's config env.
_CONFIG_ENV = {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "HERMES_HOME", "PI_CODING_AGENT_DIR",
               "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "XDG_CONFIG_HOME"}
_TEST_ENV = None


def setUpModule():
    global _TEST_ENV
    env = {k: v for k, v in os.environ.items() if k not in _CONFIG_ENV}
    env["LEOS_AGENT_PRICE_REFRESH"] = "off"
    _TEST_ENV = mock.patch.dict(os.environ, env, clear=True)
    _TEST_ENV.start()


def tearDownModule():
    _TEST_ENV.stop()


ROOT = Path(__file__).resolve().parent.parent


def load_installer():
    spec = importlib.util.spec_from_file_location("leo_install_test", ROOT / "scripts" / "leo-install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def args(**overrides):
    values = {"dry_run": False, "uninstall": False, "check": False, "force": False, "writes": True}
    values.update(overrides)
    return types.SimpleNamespace(**values)


class TestManagedBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()
        cls.block = cls.installer.build_block(ROOT)

    def test_install_is_idempotent_and_uninstall_restores_user_text(self):
        original = "# My instructions\n\nKeep this.\n"
        installed = self.installer.inject(original, self.block)
        self.assertEqual(self.installer.inject(installed, self.block), installed)
        restored = self.installer.strip_block(installed).rstrip("\n") + "\n"
        self.assertEqual(restored, original)

    def test_fenced_example_is_not_a_real_marker(self):
        original = "```md\n<leos-agent>\nexample\n</leos-agent>\n```\n"
        installed = self.installer.inject(original, self.block)
        self.assertIn(original.strip(), installed)
        self.assertEqual(installed.count('<leos-agent version="'), 1)

    def test_malformed_markers_refuse_to_edit(self):
        with self.assertRaises(self.installer.BlockError):
            self.installer.inject("<leos-agent>\nno close\n", self.block)
        with self.assertRaises(self.installer.BlockError):
            self.installer.inject("</leos-agent>\n", self.block)

    def test_longer_fence_hides_shorter_fence_markers(self):
        # CommonMark: a ``` line inside a ```` fence is content, not a closer.
        # Markers inside such an example must not be treated as a live block.
        original = "````md\n```\n<leos-agent>\nexample\n</leos-agent>\n```\n````\n"
        installed = self.installer.inject(original, self.block)
        self.assertIn(original.strip(), installed)
        self.assertEqual(installed.count('<leos-agent version="'), 1)


class TestCodexPayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    def test_install_update_and_uninstall_cover_every_declared_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(self.installer.Path, "home", return_value=home):
                first = self.installer.run("codex", ROOT, args())
                second = self.installer.run("codex", ROOT, args())

                # First target is the ~/.codex/AGENTS.md migration: there is no
                # legacy file in a fresh $HOME, so it has nothing to do on any
                # of the three runs. Only the TOML copies actually install.
                self.assertEqual([r.status for r in first], ["unchanged"] + ["created"] * len(self.installer.CODEX_AGENTS))
                self.assertEqual([r.status for r in second], ["unchanged"] * (len(self.installer.CODEX_AGENTS) + 1))
                for name in self.installer.CODEX_AGENTS:
                    installed = home / ".codex" / "agents" / f"{name}.toml"
                    source = ROOT / "payload" / "codex-agents" / f"{name}.toml"
                    self.assertEqual(installed.read_bytes(), source.read_bytes())

                removed = self.installer.run("codex", ROOT, args(uninstall=True))
                self.assertEqual([r.status for r in removed], ["unchanged"] + ["removed"] * len(self.installer.CODEX_AGENTS))
                self.assertFalse((home / ".codex" / "AGENTS.md").exists())
                for name in self.installer.CODEX_AGENTS:
                    self.assertFalse((home / ".codex" / "agents" / f"{name}.toml").exists())

    def test_foreign_agent_is_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".codex" / "agents" / "leo-runner.toml"
            target.parent.mkdir(parents=True)
            target.write_text("name = \"mine\"\n", encoding="utf-8")
            with mock.patch.object(self.installer.Path, "home", return_value=home):
                results = self.installer.run("codex", ROOT, args())
            by_target = {result.target: result for result in results}
            self.assertEqual(by_target["~/.codex/agents/leo-runner.toml"].status, "conflict")
            self.assertEqual(target.read_text(encoding="utf-8"), "name = \"mine\"\n")

    def test_codex_sources_carry_no_plugin_root_token(self):
        # The TOML copies are compared byte-for-byte against their sources in
        # the round-trip test above; a token appearing in one would make the
        # installed copy differ by design. Fail loudly here instead.
        for name in self.installer.CODEX_AGENTS:
            text = (ROOT / "payload" / "codex-agents" / f"{name}.toml").read_text(encoding="utf-8")
            self.assertNotIn(self.installer.PLUGIN_ROOT_TOKEN, text)


def isolated(installer, home):
    """Patches for a hermetic install: fake $HOME, empty machine-local config."""
    return (
        mock.patch.object(installer.Path, "home", return_value=home),
        mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(home / ".leos-agent-local")}),
    )


class TestOpenCodePayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    def run_opencode(self, home, **overrides):
        home_patch, env_patch = isolated(self.installer, home)
        with home_patch, env_patch:
            return self.installer.run("opencode", ROOT, args(**overrides))

    def test_round_trip_covers_every_copied_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # Three targets never copy anything on an unconfigured machine: the
            # AGENTS.md migration (no legacy file in a fresh $HOME), the routing
            # rule (written only when opencode routing is configured), and the
            # opencode.json advisory (never writes at all). Every other target
            # is a file copy.
            advisory_labels = {
                "~/.config/opencode/AGENTS.md",
                "~/.config/opencode/leos-agent-routing.md",
                "~/.config/opencode/opencode.json",
            }

            def copies(results):
                return [r for r in results if r.target not in advisory_labels and "/commands/" not in r.target]

            first = self.run_opencode(home)
            self.assertTrue(all(r.status == "created" for r in copies(first)), [(r.target, r.status) for r in first])
            by_target = {r.target: r for r in first}
            self.assertEqual(by_target["~/.config/opencode/AGENTS.md"].status, "unchanged")
            self.assertEqual(by_target["~/.config/opencode/opencode.json"].status, "created")

            second = self.run_opencode(home)
            self.assertTrue(
                all(r.status == "unchanged" for r in copies(second)), [(r.target, r.status) for r in second]
            )

            removed = self.run_opencode(home, uninstall=True)
            self.assertTrue(
                all(r.status in ("removed", "unchanged") for r in copies(removed)), [(r.target, r.status) for r in removed]
            )
            cfg = home / ".config" / "opencode"
            self.assertFalse((cfg / "skills" / "leo-install").exists())
            for name in self.installer.OPENCODE_SKILLS:
                self.assertFalse((cfg / "skills" / name).exists(), name)
            self.assertEqual(list((cfg / "commands").glob("*.md")), [])

    def test_uninstall_preserves_preexisting_empty_settings(self):
        from jsonc_edit import clean
        for original in ('{"plugin": [], "instructions": []}', '{}'):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                cfg = home / ".config" / "opencode"
                cfg.mkdir(parents=True)
                path = cfg / "opencode.json"
                path.write_text(original)
                for options in ({}, {}, {"uninstall": True}):
                    results = self.run_opencode(home, **options)
                    self.assertFalse(any(r.failed for r in results), str(results))
                self.assertEqual(json.loads(clean(path.read_text())), json.loads(original))

    def test_legacy_receipt_does_not_claim_ownership_of_empty_keys(self):
        from jsonc_edit import clean
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_opencode(home)
            cfg = home / ".config" / "opencode"
            receipt = cfg / "leos-agent-paths.json"
            previous = json.loads(receipt.read_text())
            previous.pop("created_keys")
            receipt.write_text(json.dumps(previous))
            results = self.run_opencode(home, uninstall=True)
            self.assertFalse(any(r.failed for r in results), str(results))
            self.assertEqual(json.loads(clean((cfg / "opencode.json").read_text())),
                             {"plugin": [], "instructions": []})

    def test_opencode_config_is_managed_without_losing_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / ".config" / "opencode"
            cfg.mkdir(parents=True)
            config_path = cfg / "opencode.json"
            config_path.write_text('{\n  // a comment the tool must not disturb\n  "theme": "dark"\n}\n', encoding="utf-8")

            results = self.run_opencode(home)
            by_target = {r.target: r for r in results}
            result = by_target["~/.config/opencode/opencode.json"]
            self.assertEqual(result.status, "updated")
            self.assertIn("leos-agent-routing.md", config_path.read_text())
            self.assertFalse(result.failed)
            self.assertIn("a comment the tool must not disturb", config_path.read_text(encoding="utf-8"))

            config_path.write_text(
                '{\n  "instructions": ["' + str(ROOT / "rules" / "preferences.md") + '"]\n}\n', encoding="utf-8"
            )
            [again] = [r for r in self.run_opencode(home) if r.target == "~/.config/opencode/opencode.json"]
            self.assertEqual(again.status, "updated")

    def test_copies_carry_absolute_root_and_no_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_opencode(home)
            token_in_some_copy = False
            for copy in sorted((home / ".config" / "opencode").rglob("*.md")):
                text = copy.read_text(encoding="utf-8")
                self.assertNotIn(self.installer.PLUGIN_ROOT_TOKEN, text, copy.name)
                # Prose may *mention* the env var; building a path from it is
                # the bug (same line test_policy draws for the sources).
                self.assertNotIn("CLAUDE_PLUGIN_ROOT}/", text, copy.name)
                if f"{ROOT}/scripts/" in text:
                    token_in_some_copy = True
            self.assertTrue(token_in_some_copy, "no copy embeds the absolute plugin root; substitution did not run")

    def test_install_skill_is_renamed_to_leo_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_opencode(home)
            copied = (home / ".config" / "opencode" / "skills" / "leo-install" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("name: leo-install\n", copied)
            self.assertNotIn("name: install\n", copied)
            self.assertIn("disable-model-invocation: true", copied)
            source = (ROOT / "skills" / "install" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("name: install\n", source)

    def test_stale_root_copy_updates_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dest = home / ".config" / "opencode" / "skills" / "doctor" / "SKILL.md"
            dest.parent.mkdir(parents=True)
            src = ROOT / "skills" / "doctor" / "SKILL.md"
            dest.write_text(self.installer.opencode_payload(src, Path("/old/fake/root")), encoding="utf-8")
            results = self.run_opencode(home)
            by_target = {r.target: r for r in results}
            self.assertEqual(by_target["~/.config/opencode/skills/doctor/SKILL.md"].status, "updated")
            self.assertIn(f"{ROOT}/scripts/", dest.read_text(encoding="utf-8"))

    def test_foreign_skill_copy_conflicts_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dest = home / ".config" / "opencode" / "skills" / "doctor" / "SKILL.md"
            dest.parent.mkdir(parents=True)
            dest.write_text("# my own notes, nothing to do with the plugin\n", encoding="utf-8")
            results = self.run_opencode(home)
            by_target = {r.target: r for r in results}
            self.assertEqual(by_target["~/.config/opencode/skills/doctor/SKILL.md"].status, "conflict")
            self.assertIn("my own notes", dest.read_text(encoding="utf-8"))


class TestAllHarnessRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    def round_trip(self, harness, home):
        home_patch, env_patch = isolated(self.installer, home)
        with home_patch, env_patch:
            first = self.installer.run(harness, ROOT, args())
            second = self.installer.run(harness, ROOT, args())
            removed = self.installer.run(harness, ROOT, args(uninstall=True))
        return first, second, removed

    def test_every_harness_round_trips(self):
        starter = "# I am Hermes\n"
        for harness in self.installer.HARNESSES:
            with self.subTest(harness=harness):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    if harness == "hermes":
                        (home / ".hermes").mkdir(parents=True)
                        (home / ".hermes" / "SOUL.md").write_text(starter, encoding="utf-8")
                    first, second, removed = self.round_trip(harness, home)
                    for phase, results in (("install", first), ("reinstall", second), ("uninstall", removed)):
                        self.assertFalse(
                            any(r.failed for r in results),
                            f"{harness} {phase}: {[(r.target, r.status, r.detail) for r in results]}",
                        )
                    self.assertFalse(
                        any(r.changed for r in second),
                        f"{harness} reinstall wrote something: {[(r.target, r.status) for r in second]}",
                    )
                    if harness == "hermes":
                        self.assertEqual((home / ".hermes" / "SOUL.md").read_text(encoding="utf-8"), starter)

    def test_hermes_installs_with_no_soul_file(self):
        # Hermes gets the payload from register_system_prompt_section now, so a
        # machine that never had SOUL.md is already correct: nothing to write,
        # and nothing to migrate away either.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            home_patch, env_patch = isolated(self.installer, home)
            with home_patch, env_patch:
                results = self.installer.run("hermes", ROOT, args())
            self.assertEqual([r.status for r in results], ["unchanged"])
            self.assertFalse((home / ".hermes").exists())


class TestOpenCodeRoutingRule(unittest.TestCase):
    """`instructions` reads preferences.md UN-rendered, so without this file a
    configured routing.json would never reach OpenCode at all."""

    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    # The normalised shape routing.load() produces; the bare-string shorthand is
    # only expanded on the way through load(), which these tests stub out.
    CONFIG = {"opencode": {"runner": {"model": "some-cheap-model", "effort": None}}}

    def run_opencode(self, home, config):
        home_patch, env_patch = isolated(self.installer, home)
        with home_patch, env_patch, mock.patch.object(self.installer.routing, "load", lambda *a, **k: config):
            return {r.target: r for r in self.installer.run("opencode", ROOT, args())}

    def test_unconfigured_installs_one_rendered_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            results = self.run_opencode(home, {})
            self.assertEqual(results["~/.config/opencode/leos-agent-routing.md"].status, "created")
            self.assertTrue((home / ".config" / "opencode" / "leos-agent-routing.md").exists())

    def test_configured_writes_the_stanza_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            results = self.run_opencode(home, self.CONFIG)
            self.assertEqual(results["~/.config/opencode/leos-agent-routing.md"].status, "created")
            body = (home / ".config" / "opencode" / "leos-agent-routing.md").read_text(encoding="utf-8")
            self.assertIn("some-cheap-model", body)
            self.assertIn(self.installer.PROVENANCE, body)
            again = self.run_opencode(home, self.CONFIG)
            self.assertEqual(again["~/.config/opencode/leos-agent-routing.md"].status, "unchanged")

    def test_native_agents_restrict_delegation_except_the_reviewer(self):
        """OpenCode can enforce this per agent; the reviewer must keep its
        lens delegation, which review-pr depends on."""
        installer = load_installer()
        for name in installer.CODEX_AGENTS:
            frontmatter = installer.native_agent(ROOT, name, "opencode", {}).split("---", 2)[1]
            with self.subTest(agent=name):
                if name == "leo-reviewer":
                    self.assertNotIn("task: false", frontmatter)
                else:
                    self.assertIn("task: false", frontmatter)

    def test_config_points_to_exactly_one_rendered_policy(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_opencode(home, self.CONFIG)
            cfg = json.loads((home / ".config/opencode/opencode.json").read_text())
            self.assertEqual(cfg["instructions"], [str(home / ".config/opencode/leos-agent-routing.md")])

    def test_unconfiguring_takes_back_our_stale_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_opencode(home, self.CONFIG)
            results = self.run_opencode(home, {})
            self.assertEqual(results["~/.config/opencode/leos-agent-routing.md"].status, "updated")
            self.assertTrue((home / ".config" / "opencode" / "leos-agent-routing.md").exists())


class TestCursorRoutingRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    def run_cursor(self, home, **overrides):
        home_patch, env_patch = isolated(self.installer, home)
        with home_patch, env_patch:
            return self.installer.run("cursor", ROOT, args(**overrides))

    def rule_path(self, home):
        return home / ".cursor" / "rules" / "leos-agent-routing.mdc"

    def write_config(self, home, body):
        local = home / ".leos-agent-local"
        local.mkdir(parents=True, exist_ok=True)
        (local / "routing.json").write_text(body, encoding="utf-8")

    def test_unconfigured_profiles_omit_the_model_key(self):
        """"inherit" is Claude Code frontmatter, not a model Cursor resolves.
        Writing it claimed a routing decision no harness was making."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            results = self.run_cursor(home)
            self.assertFalse(any(r.failed for r in results))
            self.assertFalse(self.rule_path(home).exists())
            frontmatter = (home / ".cursor/agents/leo-cheap.md").read_text().split("---", 2)[1]
            self.assertNotIn("model:", frontmatter)
            self.assertNotIn("inherit", frontmatter)

    def test_cursor_copies_carry_the_no_delegation_instruction(self):
        """Cursor has no per-agent tool restriction and subagentStart does not
        report the parent's agent type, so the worker body is the only carrier."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_cursor(home)
            worker = (home / ".cursor/agents/leo-cheap.md").read_text()
            self.assertIn("do not spawn further agents", worker.lower())
            reviewer = (home / ".cursor/agents/leo-reviewer.md").read_text()
            self.assertIn("delegate bounded specialist lenses", reviewer)

    def test_configured_profiles_round_trip_and_keep_provider_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.write_config(home, '{"cursor": {"runner": "provider/cheap-model"}}')
            self.run_cursor(home)
            profile = home / ".cursor/agents/leo-cheap.md"
            self.assertIn('model: "provider/cheap-model"', profile.read_text())
            self.assertFalse(any(r.changed for r in self.run_cursor(home)))
            self.run_cursor(home, uninstall=True)
            self.assertFalse(profile.exists())

    def test_obsolete_owned_rule_is_removed_but_foreign_rule_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rule = self.rule_path(home)
            rule.parent.mkdir(parents=True)
            rule.write_text("description: leos-agent model routing for this machine.")
            self.run_cursor(home)
            self.assertFalse(rule.exists())
            rule.parent.mkdir(parents=True, exist_ok=True)
            rule.write_text("# somebody else's rule")
            self.run_cursor(home)
            self.assertEqual(rule.read_text(), "# somebody else's rule")


class TestLegacyBlockMigration(unittest.TestCase):
    """The block is gone from every harness, so the only thing that still edits
    a user's own instruction file is taking back what an older version wrote."""

    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    # Deliberately not the current version: migration keys on the markers, not
    # on which release happened to write them.
    LEGACY = '<leos-agent version="10.0.0">\nold policy text\n</leos-agent>\n'

    def run_claude(self, home, **overrides):
        home_patch, env_patch = isolated(self.installer, home)
        with home_patch, env_patch:
            return self.installer.run("claude", ROOT, args(**overrides))

    def seed(self, home, text):
        target = home / ".claude" / "CLAUDE.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def test_block_goes_and_surrounding_text_survives_byte_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = self.seed(home, "# Mine\n\n" + self.LEGACY + "\nAfter the block.\n")
            [result] = self.run_claude(home)
            self.assertEqual(result.status, "migrated")
            after = target.read_text(encoding="utf-8")
            self.assertNotIn("<leos-agent", after)
            self.assertIn("# Mine", after)
            self.assertIn("After the block.", after)
            # Second run has nothing left to do.
            [again] = self.run_claude(home)
            self.assertEqual(again.status, "unchanged")

    def test_file_holding_only_a_block_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = self.seed(home, self.LEGACY)
            [result] = self.run_claude(home)
            self.assertEqual(result.status, "migrated")
            self.assertFalse(target.exists())

    def test_file_without_a_block_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            original = "# Just mine\n\nNothing of ours here.\n"
            target = self.seed(home, original)
            [result] = self.run_claude(home)
            self.assertEqual(result.status, "unchanged")
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_malformed_markers_are_reported_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # An opener with no closer: removing "the block" would mean guessing
            # where the user's own text resumes.
            original = '# Mine\n\n<leos-agent version="10.0.0">\nstranded\n'
            target = self.seed(home, original)
            [result] = self.run_claude(home)
            self.assertEqual(result.status, "error")
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            original = "# Mine\n\n" + self.LEGACY
            target = self.seed(home, original)
            [result] = self.run_claude(home, dry_run=True, writes=False)
            self.assertEqual(result.status, "migrated")
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_crlf_file_keeps_crlf_while_migrating(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".claude" / "CLAUDE.md"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"# Mine\r\n\r\n" + self.LEGACY.replace("\n", "\r\n").encode() + b"Keep this.\r\n")
            [first] = self.run_claude(home)
            self.assertEqual(first.status, "migrated")
            raw = target.read_bytes()
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"<leos-agent", raw)
            self.assertIn("Keep this.", raw.decode("utf-8"))
            [second] = self.run_claude(home)
            self.assertEqual(second.status, "unchanged")

    def test_symlinked_file_is_written_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            real = home / "dotfiles" / "CLAUDE.md"
            real.parent.mkdir(parents=True)
            real.write_text("# Mine\n\n" + self.LEGACY, encoding="utf-8")
            link = home / ".claude" / "CLAUDE.md"
            link.parent.mkdir(parents=True)
            link.symlink_to(real)
            [result] = self.run_claude(home)
            self.assertEqual(result.status, "migrated")
            self.assertTrue(link.is_symlink())
            body = real.read_text(encoding="utf-8")
            self.assertNotIn("<leos-agent", body)
            self.assertIn("# Mine", body)


class TestLegacyMigration(unittest.TestCase):
    def test_unchanged_old_commands_are_removed_but_edits_preserved(self):
        installer = load_installer()
        original = "---\ndescription: Stage a pending (unsubmitted) GitHub review on a pull request of this repository.\nargument-hint: \"[pr-number]\"\n---\n\nUse the leos-agent `review-pr` skill on `$ARGUMENTS`.\n\nWith no argument, review the pull request for the current branch. Comments are\nstaged as a PENDING review — never submitted, never made public.\n"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "review-pr.md"
            dest.write_text(original)
            self.assertTrue(installer.legacy_copy(original))
            installer.remove_legacy_command(dest, args(), "legacy")
            self.assertFalse(dest.exists())
            dest.write_text(original + "My change\n")
            installer.remove_legacy_command(dest, args(), "legacy")
            self.assertTrue(dest.exists())

    def test_a_baked_absolute_root_is_recovered_from_any_reference_shape(self):
        """The upgrade path turns on this. An OpenCode install rewrites
        <plugin-root> to an absolute path in every copy; the next upgrade has to
        undo that to recognise its own work by hash. Detection that only knew the
        `/scripts/*.py` shape missed the one skill file whose sole reference was
        `<plugin-root>/skills/...` in backticks, and a single unrecognised copy
        aborted the whole transaction. Roots that themselves contain a plugin
        directory name, or a space, are the cases a naive regex gets wrong."""
        installer = load_installer()
        for root in ("/home/leo/.local/share/leos-agent", "/opt/agents/leos-agent",
                     "/home/leo/skills/leos-agent",
                     "/Users/leo/Library/Application Support/leos-agent"):
            for shape in (
                'python3 "{}/scripts/handoff.py" list',
                "  python3 {}/scripts/doctor.py --harness pi",
                "read `{}/skills/review-pr/reference/procedure.md` and follow it",
                "see {}/rules/preferences.md for the policy",
            ):
                text = shape.format(root)
                with self.subTest(root=root, shape=shape):
                    self.assertIn(root, installer.candidate_roots(text))
                    self.assertEqual(
                        text.replace(root, installer.PLUGIN_ROOT_TOKEN),
                        min((v for v in (text.replace(r, installer.PLUGIN_ROOT_TOKEN)
                                         for r in installer.candidate_roots(text))
                             if installer.PLUGIN_ROOT_TOKEN in v), key=len, default=None),
                    )

    def test_a_skill_copy_whose_only_reference_is_a_skill_path_is_recognised(self):
        """The exact file that blocked the v11 upgrade: its lone <plugin-root>
        reference points at a skill, not a script, so root detection keyed on
        `/scripts/*.py` never fired and the copy read as a stranger's file."""
        installer = load_installer()
        source = (ROOT / "skills" / "review-pr" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(installer.PLUGIN_ROOT_TOKEN + "/skills/", source)
        self.assertNotIn(installer.PLUGIN_ROOT_TOKEN + "/scripts/", source)
        baked = installer.opencode_payload(ROOT / "skills" / "review-pr" / "SKILL.md",
                                           "/home/leo/.local/share/leos-agent")
        recovered = [baked.replace(r, installer.PLUGIN_ROOT_TOKEN) for r in installer.candidate_roots(baked)]
        self.assertIn(installer.opencode_payload(ROOT / "skills" / "review-pr" / "SKILL.md",
                                                 installer.PLUGIN_ROOT_TOKEN), recovered)

    def test_an_edited_copy_is_never_recognised_however_many_roots_are_offered(self):
        """Offering more candidate roots is only safe because a full-content hash
        stays the arbiter. If that ever stopped being true, this deletes work."""
        installer = load_installer()
        original = "---\ndescription: Stage a pending (unsubmitted) GitHub review on a pull request of this repository.\nargument-hint: \"[pr-number]\"\n---\n\nUse the leos-agent `review-pr` skill on `$ARGUMENTS`.\n\nWith no argument, review the pull request for the current branch. Comments are\nstaged as a PENDING review — never submitted, never made public.\n"
        self.assertTrue(installer.legacy_copy(original))
        self.assertFalse(installer.legacy_copy(original + "one appended line\n"))
        self.assertFalse(installer.legacy_copy("# a file we never wrote\n"))


if __name__ == "__main__":
    unittest.main()
