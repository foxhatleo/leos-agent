"""Behavioral tests for the cross-harness installer."""

import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


# A fake Path.home() must not be bypassed by the invoking user's config env.
_CONFIG_ENV = {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "HERMES_HOME", "PI_CODING_AGENT_DIR",
               "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "XDG_CONFIG_HOME", "LEOS_AGENT_LOCAL_PATH"}
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
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "released"
# Where each harness keeps its config under a fake $HOME when no override is set.
DIRS = {"claude": ".claude", "codex": ".codex", "cursor": ".cursor", "hermes": ".hermes",
        "pi": ".pi/agent", "opencode": ".config/opencode"}


def load_installer():
    spec = importlib.util.spec_from_file_location("leo_install_test", ROOT / "scripts" / "leo-install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def args(**overrides):
    values = {"dry_run": False, "uninstall": False, "check": False, "force": False, "writes": True}
    values.update(overrides)
    return types.SimpleNamespace(**values)


def isolated(installer, home):
    """Patches for a hermetic install: fake $HOME, empty machine-local config."""
    return (
        mock.patch.object(installer.Path, "home", return_value=home),
        mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(home / ".leos-agent-local")}),
    )


def tree(base, skip=(".leos-agent-local",)):
    """Every entry under `base`: bytes for files, the target for links."""
    out = {}
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base).as_posix()
        if rel.split("/")[0] in skip:
            continue
        if path.is_symlink():
            out[rel] = ("link", os.readlink(path))
        elif path.is_file():
            out[rel] = ("file", path.read_bytes(), path.stat().st_mode & 0o777)
        else:
            out[rel] = ("dir",)
    return out


class InstallerCase(unittest.TestCase):
    """A temp $HOME holding the named harness config directories."""

    harnesses = ()

    @classmethod
    def setUpClass(cls):
        cls.installer = load_installer()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        for harness in self.harnesses:
            (self.home / DIRS[harness]).mkdir(parents=True, exist_ok=True)

    def cfg(self, harness):
        return self.home / DIRS[harness]

    def run_harness(self, harness, root=ROOT, config=None, env=None, **overrides):
        home_patch, env_patch = isolated(self.installer, self.home)
        with contextlib.ExitStack() as stack:
            stack.enter_context(home_patch)
            stack.enter_context(env_patch)
            if env:
                stack.enter_context(mock.patch.dict(os.environ, env))
            if config is not None:
                stack.enter_context(mock.patch.object(self.installer.routing, "load", lambda *a, **k: config))
            return self.installer.run(harness, root, args(**overrides))

    def by_target(self, results):
        return {r.target: r for r in results}

    def label(self, path):
        """The report label for `path`: the installer shows paths under $HOME as ~/…."""
        return "~/" + Path(path).relative_to(self.home).as_posix()

    def assertClean(self, results, phase=""):
        self.assertFalse(any(r.failed for r in results), f"{phase}: {[(r.target, r.status, r.detail) for r in results]}")

    def cli(self, harness, *flags, env=None):
        environment = {**os.environ, "HOME": str(self.home), "LEOS_AGENT_LOCAL_PATH": str(self.home / ".leos-agent-local"),
                       "LEOS_AGENT_PRICE_REFRESH": "off", "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
        for name in ("LEOS_AGENT_ROOT", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"):
            environment.pop(name, None)
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "leo-install.py"), harness, *flags],
                              env=environment, capture_output=True, text=True, cwd=str(self.home), timeout=60)


def plugin_copy(dest, drop=()):
    """A plugin root holding what the installer reads, minus `drop`."""
    for rel in ("agents", "payload", "skills", "rules", "scripts"):
        shutil.copytree(ROOT / rel, dest / rel, ignore=shutil.ignore_patterns("__pycache__"))
    for rel in ("index.js", "package.json"):
        shutil.copyfile(ROOT / rel, dest / rel)
    for rel in drop:
        (dest / rel).unlink(missing_ok=True)
    return dest


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

    def test_a_look_alike_tag_is_not_an_opener(self):
        for line in ("<leos-agent-notes>", "<leos-agent:x>", "<leos-agentish>"):
            with self.subTest(line=line):
                self.assertIsNone(self.installer.find_block(f"# mine\n{line}\nnotes\n"))
        for line in ("<leos-agent>", '<leos-agent version="10.0.0">'):
            with self.subTest(line=line):
                self.assertIsNotNone(self.installer.find_block(f"{line}\nx\n</leos-agent>\n"))


class TestCodexPayload(InstallerCase):
    harnesses = ("codex",)

    def test_install_update_and_uninstall_cover_every_declared_agent(self):
        first = self.run_harness("codex")
        second = self.run_harness("codex")
        # First target is the ~/.codex/AGENTS.md migration: there is no legacy
        # file in a fresh $HOME, so it has nothing to do on any of the runs.
        self.assertEqual([r.status for r in first], ["unchanged"] + ["created"] * len(self.installer.CODEX_AGENTS))
        self.assertEqual([r.status for r in second], ["unchanged"] * (len(self.installer.CODEX_AGENTS) + 1))
        for name in self.installer.CODEX_AGENTS:
            installed = self.cfg("codex") / "agents" / f"{name}.toml"
            source = ROOT / "payload" / "codex-agents" / f"{name}.toml"
            self.assertEqual(installed.read_bytes(), source.read_bytes())

        removed = self.run_harness("codex", uninstall=True)
        self.assertEqual([r.status for r in removed], ["unchanged"] + ["removed"] * len(self.installer.CODEX_AGENTS))
        self.assertFalse((self.cfg("codex") / "AGENTS.md").exists())
        self.assertEqual(list(self.cfg("codex").iterdir()), [], "uninstall left something in ~/.codex")

    def test_receipt_records_the_hash_of_every_installed_file(self):
        self.run_harness("codex")
        receipt = json.loads((self.cfg("codex") / "leos-agent-paths.json").read_text())
        expected = {f"agents/{n}.toml": hashlib.sha256((self.cfg("codex") / "agents" / f"{n}.toml").read_bytes()).hexdigest()
                    for n in self.installer.CODEX_AGENTS}
        self.assertEqual(receipt["files"], expected)

    def test_a_header_does_not_make_an_edited_copy_ours(self):
        """F1: the header survives an edit. Uninstall must keep the user's
        line, a normal install must not overwrite it, and only --force does."""
        self.run_harness("codex")
        target = self.cfg("codex") / "agents" / "leo-cheap.toml"
        edited = target.read_text() + 'model = "my-custom"\n'
        target.write_text(edited)

        install = self.by_target(self.run_harness("codex"))["~/.codex/agents/leo-cheap.toml"]
        self.assertEqual(install.status, "conflict")
        removed = self.by_target(self.run_harness("codex", uninstall=True))
        self.assertEqual(removed["~/.codex/agents/leo-cheap.toml"].status, "preserved")
        self.assertEqual(removed["~/.codex/agents/leo-standard.toml"].status, "removed")
        self.assertEqual(target.read_text(), edited)
        backup = self.home / ".leos-agent-local" / "install-backups" / "codex.json"
        self.assertTrue(backup.is_file(), "uninstall must keep its backup so it can be rolled back")

        forced = self.by_target(self.run_harness("codex", force=True))["~/.codex/agents/leo-cheap.toml"]
        self.assertEqual(forced.status, "updated")
        self.assertNotIn("my-custom", target.read_text())

    def test_foreign_agent_is_not_overwritten_without_force(self):
        target = self.cfg("codex") / "agents" / "leo-cheap.toml"
        target.parent.mkdir(parents=True)
        target.write_text("name = \"mine\"\n", encoding="utf-8")
        results = self.run_harness("codex")
        self.assertEqual(self.by_target(results)["~/.codex/agents/leo-cheap.toml"].status, "conflict")
        self.assertEqual(target.read_text(encoding="utf-8"), "name = \"mine\"\n")
        self.assertFalse((self.cfg("codex") / "agents" / "leo-standard.toml").exists(), "a conflict must abort the run")

    def test_uninstall_preserves_a_foreign_file_even_with_force(self):
        """F2: uninstall used to stop on a foreign file and recommend --force,
        which then deleted it with no backup."""
        self.run_harness("codex")
        target = self.cfg("codex") / "agents" / "leo-cheap.toml"
        target.write_text('name = "mine"\n')
        for force in (False, True):
            with self.subTest(force=force):
                results = self.run_harness("codex", uninstall=True, force=force)
                self.assertClean(results, "uninstall")
                self.assertEqual(self.by_target(results)["~/.codex/agents/leo-cheap.toml"].status, "preserved")
                self.assertEqual(target.read_text(), 'name = "mine"\n')
        self.assertFalse((self.cfg("codex") / "agents" / "leo-standard.toml").exists())

    def test_a_marker_look_alike_does_not_abort_the_install(self):
        """F10: a <leos-agent-notes> line read as an opener raised BlockError."""
        notes = self.cfg("codex") / "AGENTS.md"
        notes.write_text("# mine\n<leos-agent-notes>\nkeep\n")
        results = self.run_harness("codex")
        self.assertClean(results, "install")
        self.assertEqual(notes.read_text(), "# mine\n<leos-agent-notes>\nkeep\n")

    def test_codex_sources_carry_no_plugin_root_token(self):
        # The TOML copies are compared byte-for-byte against their sources in
        # the round-trip test above; a token appearing in one would make the
        # installed copy differ by design. Fail loudly here instead.
        for name in self.installer.CODEX_AGENTS:
            text = (ROOT / "payload" / "codex-agents" / f"{name}.toml").read_text(encoding="utf-8")
            self.assertNotIn(self.installer.PLUGIN_ROOT_TOKEN, text)


class TestRetiredProfiles(InstallerCase):
    """leo-runner and leo-executor are no longer shipped. Unchanged copies an
    earlier release wrote go, whether or not the plugin tree still has their
    sources; edited or foreign copies stay."""

    harnesses = ("codex", "cursor", "opencode")
    PATHS = {"codex": "agents/{}.toml", "cursor": "agents/{}.md", "opencode": "agents/{}.md"}

    def roots(self):
        bare = plugin_copy(self.home / "plugin-without-retired", drop=[
            f"{d}/{n}.{e}" for n in self.installer.RETIRED_AGENTS
            for d, e in (("agents", "md"), ("payload/codex-agents", "toml"))])
        return {"current tree": ROOT, "sources removed": bare}

    def seed(self, harness, name, text=None):
        dest = self.cfg(harness) / self.PATHS[harness].format(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fixture = FIXTURES / harness / self.PATHS[harness].format(name)
        dest.write_bytes(fixture.read_bytes() if text is None else text.encode())
        return dest

    def test_released_copies_are_removed_without_a_receipt(self):
        for label, root in self.roots().items():
            for harness in self.harnesses:
                with self.subTest(root=label, harness=harness):
                    seeded = [self.seed(harness, name) for name in self.installer.RETIRED_AGENTS]
                    results = self.run_harness(harness, root=root)
                    self.assertClean(results, harness)
                    for path in seeded:
                        self.assertFalse(path.exists(), path)
                        self.assertEqual(self.by_target(results)[self.label(path)].status, "removed")
                    again = self.run_harness(harness, root=root)
                    self.assertFalse([r.target for r in again if r.changed], "a rerun changed something")
                    self.run_harness(harness, root=root, uninstall=True)

    def test_edited_and_foreign_copies_are_preserved_on_install_and_uninstall(self):
        for label, root in self.roots().items():
            for harness in self.harnesses:
                with self.subTest(root=label, harness=harness):
                    fixture = (FIXTURES / harness / self.PATHS[harness].format("leo-runner")).read_text()
                    edited = self.seed(harness, "leo-runner", fixture + "my extra line\n")
                    foreign = self.seed(harness, "leo-executor", "name: mine\n")
                    for options in ({}, {"uninstall": True}, {"uninstall": True, "force": True}):
                        results = self.run_harness(harness, root=root, **options)
                        self.assertClean(results, f"{harness} {options}")
                        by_target = self.by_target(results)
                        self.assertEqual(by_target[self.label(edited)].status, "preserved")
                        self.assertEqual(by_target[self.label(foreign)].status, "preserved")
                    self.assertEqual(edited.read_text(), fixture + "my extra line\n")
                    self.assertEqual(foreign.read_text(), "name: mine\n")
                    edited.unlink()
                    foreign.unlink()

    def test_retired_names_are_not_installed(self):
        for harness in self.harnesses:
            self.run_harness(harness)
            for name in self.installer.RETIRED_AGENTS:
                self.assertFalse((self.cfg(harness) / self.PATHS[harness].format(name)).exists())
        self.assertFalse(set(self.installer.RETIRED_AGENTS) & set(self.installer.CODEX_AGENTS))


class TestPreReceiptUpgrade(InstallerCase):
    """Installs from releases before receipts carry no record of their bytes."""

    harnesses = ("opencode", "codex")

    def test_a_released_copy_in_an_older_format_is_upgraded(self):
        dest = self.cfg("opencode") / "agents" / "leo-cheap.md"
        dest.parent.mkdir(parents=True)
        dest.write_bytes((FIXTURES / "opencode" / "agents" / "leo-cheap.md").read_bytes())
        results = self.run_harness("opencode")
        self.assertClean(results, "upgrade")
        self.assertIn(self.by_target(results)["~/.config/opencode/agents/leo-cheap.md"].status, ("updated", "unchanged"))
        self.assertIn("permission:\n  task: deny\n", dest.read_text())

    def test_a_routed_model_line_does_not_hide_a_released_copy(self):
        """Native copies carry the configured model; only that exact value is
        normalised away, so a hand-edited model still reads as an edit."""
        fixture = (FIXTURES / "opencode" / "agents" / "leo-runner.md").read_text()
        routed = fixture.replace("---\n\n", 'model: "prov/cheap-x"\n---\n\n', 1)
        self.assertTrue(self.installer.known_copy(routed, "prov/cheap-x"))
        self.assertFalse(self.installer.known_copy(routed, "prov/other"))
        self.assertTrue(self.installer.known_copy(fixture))

    def test_an_unrecognised_header_copy_is_a_conflict(self):
        dest = self.cfg("codex") / "agents" / "leo-cheap.toml"
        dest.parent.mkdir(parents=True)
        dest.write_text("# Managed by leos-agent.\nname = \"leo-cheap\"\ndescription = \"mine now\"\n")
        results = self.run_harness("codex")
        self.assertEqual(self.by_target(results)["~/.codex/agents/leo-cheap.toml"].status, "conflict")

    def test_the_previous_backup_counts_as_a_receipt(self):
        """The last backup holds the hash of what that install wrote."""
        dest = self.cfg("codex") / "agents" / "leo-cheap.toml"
        dest.parent.mkdir(parents=True)
        old = "# Managed by leos-agent.\nname = \"leo-cheap\"\ndescription = \"an older body\"\n"
        dest.write_text(old)
        (self.cfg("codex") / "leos-agent-install-backup.json").write_text(json.dumps({"schema": 1, "files": [
            {"path": str(dest.resolve()), "before": None, "after_sha256": hashlib.sha256(old.encode()).hexdigest(),
             "mode": 0o644}]}))
        results = self.run_harness("codex")
        self.assertClean(results, "upgrade")
        self.assertEqual(dest.read_bytes(), (ROOT / "payload" / "codex-agents" / "leo-cheap.toml").read_bytes())
        self.assertFalse((self.cfg("codex") / "leos-agent-install-backup.json").exists(), "old backup must leave the config dir")

    def test_an_edited_pre_v12_copy_is_preserved_and_the_install_continues(self):
        dest = self.cfg("opencode") / "skills" / "doctor" / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("---\nname: doctor\ndescription: Run leos-agent diagnostics, my way.\n---\n\nMine.\n")
        results = self.run_harness("opencode")
        self.assertClean(results, "install")
        self.assertEqual(self.by_target(results)["~/.config/opencode/skills/doctor/SKILL.md"].status, "preserved")
        self.assertIn("Mine.", dest.read_text())
        self.assertTrue((self.cfg("opencode") / "skills" / "handoff" / "SKILL.md").is_file())

    def test_a_stale_receipt_entry_is_taken_back_only_when_unchanged(self):
        self.run_harness("codex")
        receipt_path = self.cfg("codex") / "leos-agent-paths.json"
        receipt = json.loads(receipt_path.read_text())
        kept, gone = self.cfg("codex") / "agents" / "old-kept.toml", self.cfg("codex") / "agents" / "old-gone.toml"
        for path in (kept, gone):
            path.write_text("# Managed by leos-agent.\nname = \"x\"\n")
            receipt["files"][f"agents/{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt_path.write_text(json.dumps(receipt))
        kept.write_text(kept.read_text() + "edited\n")
        results = self.by_target(self.run_harness("codex"))
        self.assertEqual(results["~/.codex/agents/old-gone.toml"].status, "removed")
        self.assertEqual(results["~/.codex/agents/old-kept.toml"].status, "preserved")
        self.assertFalse(gone.exists())
        self.assertTrue(kept.exists())
        self.assertNotIn("agents/old-gone.toml", json.loads(receipt_path.read_text())["files"])

    def test_a_receipt_cannot_name_a_file_outside_the_config_dir(self):
        self.run_harness("codex")
        outside = self.home / "outside.txt"
        outside.write_text("not yours\n")
        receipt_path = self.cfg("codex") / "leos-agent-paths.json"
        receipt = json.loads(receipt_path.read_text())
        for rel in ("../outside.txt", "agents/../../outside.txt", str(outside)):
            receipt["files"][rel] = hashlib.sha256(outside.read_bytes()).hexdigest()
        receipt_path.write_text(json.dumps(receipt))
        for options in ({}, {"uninstall": True}):
            self.assertClean(self.run_harness("codex", **options), str(options))
            self.assertTrue(outside.exists())


class TestOpenCodePayload(InstallerCase):
    harnesses = ("opencode",)

    def test_round_trip_covers_every_copied_file_and_restores_the_tree(self):
        before = tree(self.home)
        first = self.run_harness("opencode")
        self.assertClean(first, "install")
        by_target = self.by_target(first)
        self.assertEqual(by_target["~/.config/opencode/AGENTS.md"].status, "unchanged")
        self.assertEqual(by_target["~/.config/opencode/opencode.json"].status, "created")
        copies = [r for r in first if "/commands/" not in r.target and r.target != "~/.config/opencode/AGENTS.md"]
        self.assertTrue(all(r.status == "created" for r in copies), [(r.target, r.status) for r in first])

        installed = tree(self.home)
        second = self.run_harness("opencode")
        self.assertFalse([r.target for r in second if r.changed])
        self.assertEqual(tree(self.home), installed, "a rerun changed bytes")

        removed = self.run_harness("opencode", uninstall=True)
        self.assertClean(removed, "uninstall")
        self.assertEqual(tree(self.home), before, "uninstall did not restore the config directory")

    def test_uninstall_preserves_preexisting_empty_settings(self):
        from jsonc_edit import clean
        for original in ('{"plugin": [], "instructions": []}', '{}'):
            with self.subTest(original=original):
                path = self.cfg("opencode") / "opencode.json"
                path.write_text(original)
                for options in ({}, {}, {"uninstall": True}):
                    self.assertClean(self.run_harness("opencode", **options), str(options))
                self.assertEqual(json.loads(clean(path.read_text())), json.loads(original))
                self.assertEqual(path.read_text(), original, "uninstall did not restore the bytes")

    def test_legacy_receipt_does_not_claim_ownership_of_empty_keys(self):
        from jsonc_edit import clean
        self.run_harness("opencode")
        receipt = self.cfg("opencode") / "leos-agent-paths.json"
        previous = json.loads(receipt.read_text())
        # The shape a release before receipts wrote.
        receipt.write_text(json.dumps({"instructions": previous["instructions"], "plugin": previous["plugin"]}))
        results = self.run_harness("opencode", uninstall=True)
        self.assertClean(results, "uninstall")
        self.assertEqual(json.loads(clean((self.cfg("opencode") / "opencode.json").read_text())),
                         {"plugin": [], "instructions": []})

    def test_opencode_config_is_managed_without_losing_comments(self):
        config_path = self.cfg("opencode") / "opencode.json"
        original = '{\n  // a comment the tool must not disturb\n  "theme": "dark"\n}\n'
        config_path.write_text(original, encoding="utf-8")
        result = self.by_target(self.run_harness("opencode"))["~/.config/opencode/opencode.json"]
        self.assertEqual(result.status, "updated")
        text = config_path.read_text(encoding="utf-8")
        self.assertIn("leos-agent-routing.md", text)
        self.assertIn("a comment the tool must not disturb", text)
        self.assertNotIn("\n,", text, "a comma on its own line")
        self.run_harness("opencode", uninstall=True)
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)

        config_path.write_text('{\n  "instructions": ["' + str(ROOT / "rules" / "preferences.md") + '"]\n}\n', encoding="utf-8")
        again = self.by_target(self.run_harness("opencode"))["~/.config/opencode/opencode.json"]
        self.assertEqual(again.status, "updated")
        self.assertNotIn("preferences.md", config_path.read_text())

    def test_repeated_cycles_do_not_grow_the_config(self):
        """F7: erased characters used to become spaces, so each cycle grew it."""
        path = self.cfg("opencode") / "opencode.json"
        for original in ('{"model":"x"}', '{\n  "model": "x"\n}\n', '{\n  "model": "x", // why\n}\n'):
            with self.subTest(original=original):
                path.write_text(original)
                for _ in range(4):
                    self.run_harness("opencode")
                    self.run_harness("opencode", uninstall=True)
                self.assertEqual(path.read_text(), original)

    def test_an_absent_config_is_created_and_then_deleted(self):
        path = self.cfg("opencode") / "opencode.json"
        self.run_harness("opencode")
        self.assertTrue(path.is_file())
        result = self.by_target(self.run_harness("opencode", uninstall=True))["~/.config/opencode/opencode.json"]
        self.assertEqual(result.status, "removed")
        self.assertFalse(path.exists())

    def test_a_created_config_the_user_added_to_is_kept(self):
        path = self.cfg("opencode") / "opencode.json"
        self.run_harness("opencode")
        text = path.read_text()
        path.write_text(text.replace("{\n", '{\n  "theme": "dark",\n', 1))
        self.run_harness("opencode", uninstall=True)
        self.assertEqual(json.loads(path.read_text()), {"theme": "dark"})

    def test_crlf_config_keeps_crlf(self):
        """F8."""
        path = self.cfg("opencode") / "opencode.json"
        path.write_bytes(b'{\r\n  "model": "x"\r\n}\r\n')
        self.run_harness("opencode")
        raw = path.read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.run_harness("opencode", uninstall=True)
        self.assertEqual(path.read_bytes(), b'{\r\n  "model": "x"\r\n}\r\n')

    def test_dry_run_shows_the_config_diff(self):
        results = self.by_target(self.run_harness("opencode", dry_run=True, writes=False))
        diff = results["~/.config/opencode/opencode.json"].diff
        self.assertIn('+  "instructions"', diff)
        self.assertFalse((self.cfg("opencode") / "opencode.json").exists())

    def test_copies_carry_absolute_root_and_no_placeholders(self):
        self.run_harness("opencode")
        token_in_some_copy = False
        for copy in sorted(self.cfg("opencode").rglob("*.md")):
            text = copy.read_text(encoding="utf-8")
            self.assertNotIn(self.installer.PLUGIN_ROOT_TOKEN, text, copy.name)
            # Prose may *mention* the env var; building a path from it is
            # the bug (same line test_policy draws for the sources).
            self.assertNotIn("CLAUDE_PLUGIN_ROOT}/", text, copy.name)
            if f"{ROOT}/scripts/" in text:
                token_in_some_copy = True
        self.assertTrue(token_in_some_copy, "no copy embeds the absolute plugin root; substitution did not run")

    def test_install_skill_is_renamed_to_leo_install(self):
        self.run_harness("opencode")
        copied = (self.cfg("opencode") / "skills" / "leo-install" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: leo-install\n", copied)
        self.assertNotIn("name: install\n", copied)
        self.assertIn("disable-model-invocation: true", copied)
        source = (ROOT / "skills" / "install" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: install\n", source)

    def test_stale_root_copy_updates_in_place(self):
        dest = self.cfg("opencode") / "skills" / "doctor" / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text(self.installer.opencode_payload(ROOT / "skills" / "doctor" / "SKILL.md", Path("/old/fake/root")))
        # A receipt from the install that wrote it.
        (self.cfg("opencode") / "leos-agent-paths.json").write_text(json.dumps(
            {"files": {"skills/doctor/SKILL.md": hashlib.sha256(dest.read_bytes()).hexdigest()}}))
        results = self.by_target(self.run_harness("opencode"))
        self.assertEqual(results["~/.config/opencode/skills/doctor/SKILL.md"].status, "updated")
        self.assertIn(f"{ROOT}/scripts/", dest.read_text(encoding="utf-8"))

    def test_foreign_skill_copy_conflicts_without_force(self):
        dest = self.cfg("opencode") / "skills" / "doctor" / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("# my own notes, nothing to do with the plugin\n", encoding="utf-8")
        results = self.by_target(self.run_harness("opencode"))
        self.assertEqual(results["~/.config/opencode/skills/doctor/SKILL.md"].status, "conflict")
        self.assertIn("my own notes", dest.read_text(encoding="utf-8"))

    def test_an_edited_command_with_our_header_is_kept(self):
        """F1: a header-bearing command an install used to delete."""
        command = self.cfg("opencode") / "commands" / "handoff.md"
        command.parent.mkdir(parents=True)
        command.write_text("<!-- Managed by leos-agent. -->\nmy edited handoff command\n")
        for options in ({}, {"uninstall": True}):
            results = self.by_target(self.run_harness("opencode", **options))
            self.assertEqual(results["~/.config/opencode/commands/handoff.md"].status, "preserved")
        self.assertTrue(command.exists())


class TestOpenCodeConfigLocation(InstallerCase):
    harnesses = ("opencode",)

    def test_uninstall_edits_the_file_the_install_edited(self):
        """F5: OPENCODE_CONFIG was set for the install and not for uninstall."""
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        target = elsewhere / "opencode.json"
        target.write_text("{}\n")
        self.assertClean(self.run_harness("opencode", env={"OPENCODE_CONFIG": str(target)}), "install")
        self.assertIn("index.js", target.read_text())
        self.assertFalse((self.cfg("opencode") / "opencode.json").exists())
        self.assertClean(self.run_harness("opencode", uninstall=True), "uninstall")
        self.assertEqual(target.read_text(), "{}\n")

    def test_a_new_jsonc_takes_over_the_registration(self):
        json_path = self.cfg("opencode") / "opencode.json"
        self.run_harness("opencode")
        self.assertTrue(json_path.is_file())
        jsonc = self.cfg("opencode") / "opencode.jsonc"
        jsonc.write_text("{\n  // mine\n}\n")
        results = self.by_target(self.run_harness("opencode"))
        self.assertEqual(results["~/.config/opencode/opencode.json"].status, "removed")
        self.assertIn("index.js", jsonc.read_text())
        self.assertFalse(json_path.exists())
        self.run_harness("opencode", uninstall=True)
        self.assertEqual(jsonc.read_text(), "{\n  // mine\n}\n")

    def test_opencode_config_parent_must_exist(self):
        target = self.home / "nope" / "deeper" / "opencode.json"
        results = self.run_harness("opencode", env={"OPENCODE_CONFIG": str(target)})
        self.assertTrue(any(r.failed for r in results))
        self.assertFalse((self.home / "nope").exists())
        self.assertFalse((self.cfg("opencode") / "agents").exists(), "a failed run wrote copies")

    def test_an_npm_cache_root_keeps_the_native_spec(self):
        """A file:// link into OpenCode's package cache breaks when the cache is
        cleaned; the spec makes OpenCode fetch the package again."""
        cache_root = plugin_copy(self.home / ".cache/opencode/packages/leos-agent/node_modules/leos-agent")
        path = self.cfg("opencode") / "opencode.json"
        path.write_text('{\n  "plugin": ["leos-agent"]\n}\n')
        self.assertClean(self.run_harness("opencode", root=cache_root), "install")
        plugin = json.loads(path.read_text())["plugin"]
        self.assertEqual(plugin, ["leos-agent"])
        self.run_harness("opencode", root=cache_root, uninstall=True)
        self.assertEqual(path.read_text(), '{\n  "plugin": ["leos-agent"]\n}\n')

        # A release before this one replaced the spec with a link; put it back.
        path.write_text('{\n  "plugin": ["' + (cache_root / "index.js").as_uri() + '"]\n}\n')
        (self.cfg("opencode") / "leos-agent-paths.json").write_text(json.dumps(
            {"plugin": [(cache_root / "index.js").as_uri()], "instructions": []}))
        self.run_harness("opencode", root=cache_root)
        self.assertEqual(json.loads(path.read_text())["plugin"], ["leos-agent"])

    def test_a_checkout_root_replaces_an_npm_spec_with_its_link(self):
        path = self.cfg("opencode") / "opencode.json"
        path.write_text('{"plugin": ["leos-agent@12.0.0"]}')
        self.run_harness("opencode")
        self.assertEqual(json.loads(path.read_text())["plugin"], [(ROOT / "index.js").resolve().as_uri()])


class TestConfigDirectories(InstallerCase):
    def test_a_missing_config_dir_is_an_error_and_nothing_is_created(self):
        """F3: a mistyped override used to 'succeed' by creating the directory."""
        for harness in self.installer.COPYING:
            for options in ({}, {"uninstall": True}, {"check": True, "writes": False}):
                with self.subTest(harness=harness, options=options):
                    results = self.run_harness(harness, **options)
                    self.assertEqual([r.status for r in results], ["error"])
                    self.assertIn("never creates", results[0].detail)
                    self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])

    def test_migration_only_harnesses_have_nothing_to_do_without_a_config_dir(self):
        for harness in ("claude", "hermes", "pi"):
            for options in ({}, {"uninstall": True}, {"check": True, "writes": False}):
                with self.subTest(harness=harness, options=options):
                    results = self.run_harness(harness, **options)
                    self.assertEqual([r.status for r in results], ["unchanged"])
                    self.assertIn("does not exist", results[0].detail)
                    self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])

    def test_a_missing_override_dir_is_an_error(self):
        missing = self.home / "typo"
        for harness, variable in self.installer.OVERRIDES.items():
            if harness not in self.installer.COPYING:
                continue
            with self.subTest(harness=harness):
                results = self.run_harness(harness, env={variable: str(missing)})
                self.assertEqual([r.status for r in results], ["error"])
                self.assertFalse(missing.exists())

    def test_relative_overrides_are_refused(self):
        variables = list(self.installer.OVERRIDES.values()) + ["XDG_CONFIG_HOME", "OPENCODE_CONFIG", "LEOS_AGENT_LOCAL_PATH"]
        for variable in variables:
            with self.subTest(variable=variable):
                (self.home / ".config" / "opencode").mkdir(parents=True, exist_ok=True)
                harness = next((h for h, v in self.installer.OVERRIDES.items() if v == variable), "opencode")
                (self.home / DIRS[harness]).mkdir(parents=True, exist_ok=True)
                results = self.run_harness(harness, env={variable: "relative/dir"})
                self.assertTrue(any(r.failed and "absolute" in r.detail for r in results), results)

    def test_an_empty_xdg_config_home_means_the_default(self):
        """F6: os.environ.get(..., default) returned "" and wrote into the cwd."""
        cwd = self.home / "cwd"
        cwd.mkdir()
        (self.home / ".config" / "opencode").mkdir(parents=True)
        previous = os.getcwd()
        os.chdir(cwd)
        try:
            results = self.run_harness("opencode", env={"XDG_CONFIG_HOME": ""})
        finally:
            os.chdir(previous)
        self.assertClean(results, "install")
        self.assertEqual(list(cwd.iterdir()), [])
        self.assertTrue((self.home / ".config" / "opencode" / "agents" / "leo-cheap.md").is_file())


class TestBackupsAndRollback(InstallerCase):
    harnesses = ("codex", "opencode")

    def backup(self, harness):
        return self.home / ".leos-agent-local" / "install-backups" / f"{harness}.json"

    def test_backups_live_in_the_data_dir_with_private_modes(self):
        """F4: the backup sat in the config dir holding base64 config bytes."""
        path = self.cfg("opencode") / "opencode.json"
        path.write_text('{"provider": {"x": {"options": {"apiKey": "SECRET"}}}}\n')
        self.run_harness("opencode")
        self.assertFalse(list(self.cfg("opencode").glob("*backup*")))
        backup = self.backup("opencode")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)

    def test_a_legacy_backup_is_moved_and_still_rolls_back(self):
        self.run_harness("codex")
        backup = self.backup("codex")
        legacy = self.cfg("codex") / "leos-agent-install-backup.json"
        backup.rename(legacy)
        result = self.cli("codex", "--rollback", env={"CODEX_HOME": str(self.cfg("codex"))})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.cfg("codex").iterdir()), [], "rollback left a file in the config dir")

        self.run_harness("codex")
        self.backup("codex").rename(legacy)
        self.run_harness("codex")  # nothing to change; the old backup still moves
        self.assertFalse(legacy.exists())
        self.assertTrue(self.backup("codex").is_file())

    def test_a_failed_commit_keeps_the_last_good_backup(self):
        """F9: the failed commit replaced the backup, so --rollback restored 0
        files and the first install could no longer be undone."""
        self.run_harness("opencode")
        first_backup = self.backup("opencode").read_bytes()
        doctor = self.cfg("opencode") / "skills" / "doctor"
        shutil.rmtree(doctor)
        doctor.write_text("now a file")
        results = self.run_harness("opencode", config={"opencode": {"cheap": {"model": "m-x", "effort": None}}})
        statuses = {r.status for r in results}
        self.assertIn("error", statuses)
        self.assertGreater(len(results), 2, "the report collapsed to one line")
        self.assertEqual(self.backup("opencode").read_bytes(), first_backup)
        doctor.unlink()
        result = self.cli("opencode", "--rollback", env={"XDG_CONFIG_HOME": str(self.home / ".config")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("restored 0 files", result.stdout)
        self.assertFalse((self.cfg("opencode") / "agents").exists())

    def test_rollback_with_nothing_to_roll_back_says_so(self):
        result = self.cli("codex", "--rollback", env={"CODEX_HOME": str(self.cfg("codex"))})
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing to roll back", result.stderr)
        self.assertNotIn("Errno", result.stderr)

    def test_an_uninstall_can_be_rolled_back(self):
        self.run_harness("codex")
        installed = tree(self.cfg("codex"))
        self.run_harness("codex", uninstall=True)
        result = self.cli("codex", "--rollback", env={"CODEX_HOME": str(self.cfg("codex"))})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(tree(self.cfg("codex")), installed)


class TestAllHarnessRoundTrip(InstallerCase):
    harnesses = tuple(DIRS)

    def test_every_harness_round_trips(self):
        starter = "# I am Hermes\n"
        (self.cfg("hermes") / "SOUL.md").write_text(starter, encoding="utf-8")
        for harness in self.installer.HARNESSES:
            with self.subTest(harness=harness):
                before = tree(self.cfg(harness))
                first = self.run_harness(harness)
                second = self.run_harness(harness)
                removed = self.run_harness(harness, uninstall=True)
                for phase, results in (("install", first), ("reinstall", second), ("uninstall", removed)):
                    self.assertClean(results, f"{harness} {phase}")
                self.assertFalse(any(r.changed for r in second),
                                 f"{harness} reinstall wrote something: {[(r.target, r.status) for r in second]}")
                self.assertEqual(tree(self.cfg(harness)), before, f"{harness} uninstall left a difference")
        self.assertEqual((self.cfg("hermes") / "SOUL.md").read_text(encoding="utf-8"), starter)

    def test_hermes_installs_with_no_soul_file(self):
        # Hermes gets the payload from register_system_prompt_section, so a
        # machine with ~/.hermes but no SOUL.md is already correct.
        results = self.run_harness("hermes")
        self.assertEqual([r.status for r in results], ["unchanged"])
        self.assertEqual(list(self.cfg("hermes").iterdir()), [])


class TestOpenCodeTurnCapUpgrade(InstallerCase):
    """The release before `steps` rendered OpenCode agents without it. Its own
    receipt is the evidence those copies are ours, so an upgrade replaces them
    and a later uninstall takes the new ones back."""

    harnesses = ("opencode",)

    def previous_release(self):
        current = self.installer.native_agent

        def render(root, name, harness, config):
            return re.sub(r"(?m)^steps: [0-9]+\n", "", current(root, name, harness, config))
        return mock.patch.object(self.installer, "native_agent", render)

    def test_copies_the_previous_release_wrote_are_upgraded_to_carry_steps(self):
        cap = re.search(r"(?m)^maxTurns: ([0-9]+)$", (ROOT / "agents" / "leo-cheap.md").read_text()).group(1)
        for config in ({}, {"opencode": {"cheap": {"model": "prov/cheap-x", "effort": None}}}):
            with self.subTest(configured=bool(config)):
                with self.previous_release():
                    self.assertClean(self.run_harness("opencode", config=config), "previous release")
                agent = self.cfg("opencode") / "agents" / "leo-cheap.md"
                self.assertNotIn("\nsteps:", agent.read_text())
                results = self.run_harness("opencode", config=config)
                self.assertClean(results, "upgrade")
                self.assertEqual(self.by_target(results)[self.label(agent)].status, "updated")
                self.assertIn("\nsteps: %s\n" % cap, agent.read_text())
                self.assertFalse([r.target for r in self.run_harness("opencode", config=config) if r.changed])
                self.assertClean(self.run_harness("opencode", config=config, uninstall=True), "uninstall")
                self.assertFalse(agent.exists())

    def test_an_edited_previous_copy_is_still_a_conflict(self):
        with self.previous_release():
            self.run_harness("opencode")
        agent = self.cfg("opencode") / "agents" / "leo-standard.md"
        edited = agent.read_text() + "my own note\n"
        agent.write_text(edited)
        results = self.by_target(self.run_harness("opencode"))
        self.assertEqual(results[self.label(agent)].status, "conflict")
        self.assertEqual(agent.read_text(), edited)


class TestOpenCodeRoutingRule(InstallerCase):
    """`instructions` reads preferences.md UN-rendered, so without this file a
    configured routing.json would never reach OpenCode at all."""

    harnesses = ("opencode",)
    # The normalised shape routing.load() produces; the bare-string shorthand is
    # only expanded on the way through load(), which these tests stub out.
    CONFIG = {"opencode": {"runner": {"model": "some-cheap-model", "effort": None}}}
    RULE = "~/.config/opencode/leos-agent-routing.md"

    def test_unconfigured_installs_one_rendered_policy(self):
        results = self.by_target(self.run_harness("opencode", config={}))
        self.assertEqual(results[self.RULE].status, "created")
        self.assertTrue((self.cfg("opencode") / "leos-agent-routing.md").exists())

    def test_configured_writes_the_stanza_and_is_idempotent(self):
        results = self.by_target(self.run_harness("opencode", config=self.CONFIG))
        self.assertEqual(results[self.RULE].status, "created")
        body = (self.cfg("opencode") / "leos-agent-routing.md").read_text(encoding="utf-8")
        self.assertIn("some-cheap-model", body)
        self.assertIn(self.installer.PROVENANCE, body)
        again = self.by_target(self.run_harness("opencode", config=self.CONFIG))
        self.assertEqual(again[self.RULE].status, "unchanged")

    def test_native_agents_restrict_delegation_except_the_reviewer(self):
        """OpenCode's agent schema maps this frontmatter to permission.task;
        the reviewer must keep its lens delegation, which review-pr depends on."""
        for name in self.installer.CODEX_AGENTS:
            frontmatter = self.installer.native_agent(ROOT, name, "opencode", {}).split("---", 2)[1]
            with self.subTest(agent=name):
                self.assertNotIn("tools:", frontmatter, "deprecated tools key")
                if name == "leo-reviewer":
                    self.assertNotIn("permission:", frontmatter)
                else:
                    self.assertIn("\npermission:\n  task: deny\n", frontmatter)

    def test_only_the_review_lens_is_denied_edits(self):
        """OpenCode hides its edit, write and apply_patch tools under
        permission.edit deny. The lens keeps bash for its gh and git reads."""
        self.assertClean(self.run_harness("opencode"), "install")
        for name in self.installer.CODEX_AGENTS:
            frontmatter = (self.cfg("opencode") / "agents" / f"{name}.md").read_text().split("---", 2)[1]
            with self.subTest(agent=name):
                self.assertNotIn("bash:", frontmatter)
                if name == "leo-lens":
                    self.assertIn("\npermission:\n  task: deny\n  edit: deny\n", frontmatter)
                else:
                    self.assertNotIn("edit:", frontmatter)

    def test_config_points_to_exactly_one_rendered_policy(self):
        self.run_harness("opencode", config=self.CONFIG)
        cfg = json.loads((self.cfg("opencode") / "opencode.json").read_text())
        self.assertEqual(cfg["instructions"], [str(self.cfg("opencode") / "leos-agent-routing.md")])

    def test_unconfiguring_takes_back_our_stale_rule(self):
        self.run_harness("opencode", config=self.CONFIG)
        results = self.by_target(self.run_harness("opencode", config={}))
        self.assertEqual(results[self.RULE].status, "updated")
        self.assertTrue((self.cfg("opencode") / "leos-agent-routing.md").exists())

    def test_a_pre_receipt_routing_rule_is_recognised_by_its_registration(self):
        """Its body is rendered from routing config, so no frozen hash covers a
        configured one; the old receipt listing it is the evidence."""
        rule = self.cfg("opencode") / "leos-agent-routing.md"
        rule.write_text("<!-- Managed by leos-agent. -->\nan older rendering for some-model\n")
        (self.cfg("opencode") / "leos-agent-paths.json").write_text(json.dumps(
            {"instructions": [str(rule)], "plugin": []}))
        results = self.by_target(self.run_harness("opencode", config={}))
        self.assertEqual(results[self.RULE].status, "updated")


class TestCursorRoutingRule(InstallerCase):
    harnesses = ("cursor",)

    def rule_path(self):
        return self.cfg("cursor") / "rules" / "leos-agent-routing.mdc"

    def write_config(self, body):
        local = self.home / ".leos-agent-local"
        local.mkdir(parents=True, exist_ok=True)
        (local / "routing.json").write_text(body, encoding="utf-8")

    def test_unconfigured_profiles_omit_the_model_key(self):
        """"inherit" is Claude Code frontmatter, not a model Cursor resolves.
        Writing it claimed a routing decision no harness was making."""
        results = self.run_harness("cursor")
        self.assertClean(results, "install")
        self.assertFalse(self.rule_path().exists())
        frontmatter = (self.cfg("cursor") / "agents/leo-cheap.md").read_text().split("---", 2)[1]
        self.assertNotIn("model:", frontmatter)
        self.assertNotIn("inherit", frontmatter)

    def test_cursor_copies_carry_the_no_delegation_instruction(self):
        """Cursor has no per-agent tool restriction and subagentStart does not
        report the parent's agent type, so the worker body is the only carrier."""
        self.run_harness("cursor")
        worker = (self.cfg("cursor") / "agents/leo-cheap.md").read_text()
        self.assertIn("do not spawn further agents", worker.lower())
        reviewer = (self.cfg("cursor") / "agents/leo-reviewer.md").read_text()
        self.assertIn("delegate bounded specialist lenses", reviewer)

    def test_the_review_lens_is_marked_readonly_and_round_trips(self):
        """`readonly: true` is the agent frontmatter Cursor's own plugin
        templates use; no other profile gets it."""
        self.write_config('{"cursor": {"standard": "provider/standard-model"}}')
        self.assertClean(self.run_harness("cursor"), "install")
        lens = self.cfg("cursor") / "agents/leo-lens.md"
        fields = lens.read_text().split("---", 2)[1]
        self.assertIn("\nreadonly: true\n", fields)
        self.assertIn('\nmodel: "provider/standard-model"\n', fields)
        for name in self.installer.CODEX_AGENTS:
            if name != "leo-lens":
                with self.subTest(agent=name):
                    self.assertNotIn("readonly", (self.cfg("cursor") / f"agents/{name}.md").read_text().split("---", 2)[1])
        self.assertFalse(any(r.changed for r in self.run_harness("cursor")))
        self.assertClean(self.run_harness("cursor", uninstall=True), "uninstall")
        self.assertFalse(lens.exists())

    def test_a_users_own_leo_lens_is_never_taken_over(self):
        lens = self.cfg("cursor") / "agents/leo-lens.md"
        lens.parent.mkdir(parents=True)
        mine = "---\nname: leo-lens\ndescription: my lens\n---\nmine\n"
        lens.write_text(mine)
        results = self.by_target(self.run_harness("cursor"))
        self.assertEqual(results[self.label(lens)].status, "conflict")
        # The transaction writes nothing until the user moves their file.
        self.assertFalse((self.cfg("cursor") / "agents/leo-cheap.md").exists())
        self.assertEqual(lens.read_text(), mine)
        self.assertEqual(self.by_target(self.run_harness("cursor", uninstall=True))[self.label(lens)].status, "preserved")
        self.assertEqual(lens.read_text(), mine)

    def test_configured_profiles_round_trip_and_keep_provider_identifiers(self):
        self.write_config('{"cursor": {"runner": "provider/cheap-model"}}')
        self.run_harness("cursor")
        profile = self.cfg("cursor") / "agents/leo-cheap.md"
        self.assertIn('model: "provider/cheap-model"', profile.read_text())
        self.assertFalse(any(r.changed for r in self.run_harness("cursor")))
        self.run_harness("cursor", uninstall=True)
        self.assertFalse(profile.exists())

    def test_obsolete_rule_goes_only_when_unchanged(self):
        """The rule a pre-v12 release wrote never loaded. An unchanged copy
        goes; anything else at that path is kept."""
        rule = self.rule_path()
        rule.parent.mkdir(parents=True)
        released = (FIXTURES / "cursor" / "rules" / "leos-agent-routing.mdc").read_text()
        rule.write_text(released)
        results = self.by_target(self.run_harness("cursor"))
        self.assertEqual(results[self.label(rule)].status, "removed")
        self.assertFalse(rule.exists())
        self.assertFalse(rule.parent.exists(), "the emptied rules/ directory should go too")
        rule.parent.mkdir()
        for text in (released + "my note\n", "# somebody else's rule"):
            rule.write_text(text)
            results = self.by_target(self.run_harness("cursor"))
            self.assertEqual(results[self.label(rule)].status, "preserved")
            self.assertEqual(rule.read_text(), text)


class TestLegacyBlockMigration(InstallerCase):
    """The block is gone from every harness, so the only thing that still edits
    a user's own instruction file is taking back what an older version wrote."""

    harnesses = ("claude",)
    # Deliberately not the current version: migration keys on the markers, not
    # on which release happened to write them.
    LEGACY = '<leos-agent version="10.0.0">\nold policy text\n</leos-agent>\n'

    def seed(self, text):
        target = self.cfg("claude") / "CLAUDE.md"
        target.write_text(text, encoding="utf-8")
        return target

    def test_block_goes_and_surrounding_text_survives_byte_exact(self):
        target = self.seed("# Mine\n\n" + self.LEGACY + "\nAfter the block.\n")
        [result] = self.run_harness("claude")
        self.assertEqual(result.status, "migrated")
        after = target.read_text(encoding="utf-8")
        self.assertNotIn("<leos-agent", after)
        self.assertIn("# Mine", after)
        self.assertIn("After the block.", after)
        [again] = self.run_harness("claude")
        self.assertEqual(again.status, "unchanged")

    def test_file_holding_only_a_block_is_deleted(self):
        target = self.seed(self.LEGACY)
        [result] = self.run_harness("claude")
        self.assertEqual(result.status, "migrated")
        self.assertFalse(target.exists())

    def test_file_without_a_block_is_left_alone(self):
        original = "# Just mine\n\nNothing of ours here.\n"
        target = self.seed(original)
        [result] = self.run_harness("claude")
        self.assertEqual(result.status, "unchanged")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_malformed_markers_are_reported_and_nothing_is_written(self):
        # An opener with no closer: removing "the block" would mean guessing
        # where the user's own text resumes.
        original = '# Mine\n\n<leos-agent version="10.0.0">\nstranded\n'
        target = self.seed(original)
        [result] = self.run_harness("claude")
        self.assertEqual(result.status, "error")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_dry_run_reports_without_writing(self):
        original = "# Mine\n\n" + self.LEGACY
        target = self.seed(original)
        [result] = self.run_harness("claude", dry_run=True, writes=False)
        self.assertEqual(result.status, "migrated")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_crlf_file_keeps_crlf_while_migrating(self):
        target = self.cfg("claude") / "CLAUDE.md"
        target.write_bytes(b"# Mine\r\n\r\n" + self.LEGACY.replace("\n", "\r\n").encode() + b"Keep this.\r\n")
        [first] = self.run_harness("claude")
        self.assertEqual(first.status, "migrated")
        raw = target.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"<leos-agent", raw)
        self.assertIn("Keep this.", raw.decode("utf-8"))
        [second] = self.run_harness("claude")
        self.assertEqual(second.status, "unchanged")

    def test_symlinked_file_is_written_through(self):
        real = self.home / "dotfiles" / "CLAUDE.md"
        real.parent.mkdir(parents=True)
        real.write_text("# Mine\n\n" + self.LEGACY, encoding="utf-8")
        link = self.cfg("claude") / "CLAUDE.md"
        link.symlink_to(real)
        [result] = self.run_harness("claude")
        self.assertEqual(result.status, "migrated")
        self.assertTrue(link.is_symlink())
        body = real.read_text(encoding="utf-8")
        self.assertNotIn("<leos-agent", body)
        self.assertIn("# Mine", body)

    def test_a_symlinked_file_holding_only_the_block_keeps_its_link(self):
        """F11: the link was removed and the target kept the block."""
        real = self.home / "dotfiles" / "CLAUDE.md"
        real.parent.mkdir(parents=True)
        real.write_text(self.LEGACY, encoding="utf-8")
        link = self.cfg("claude") / "CLAUDE.md"
        link.symlink_to(real)
        [result] = self.run_harness("claude")
        self.assertEqual(result.status, "migrated")
        self.assertTrue(link.is_symlink())
        self.assertNotIn("<leos-agent", real.read_text(encoding="utf-8"))


class TestLegacyMigration(unittest.TestCase):
    def test_unchanged_old_commands_are_removed_but_edits_preserved(self):
        installer = load_installer()
        original = "---\ndescription: Stage a pending (unsubmitted) GitHub review on a pull request of this repository.\nargument-hint: \"[pr-number]\"\n---\n\nUse the leos-agent `review-pr` skill on `$ARGUMENTS`.\n\nWith no argument, review the pull request for the current branch. Comments are\nstaged as a PENDING review — never submitted, never made public.\n"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "review-pr.md"
            dest.write_text(original)
            self.assertTrue(installer.known_copy(original))
            installer.remove_legacy_command(dest, args(), "legacy")
            self.assertFalse(dest.exists())
            dest.write_text(original + "My change\n")
            self.assertEqual(installer.remove_legacy_command(dest, args(), "legacy").status, "preserved")
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
        self.assertTrue(installer.known_copy(original))
        self.assertFalse(installer.known_copy(original + "one appended line\n"))
        self.assertFalse(installer.known_copy("# a file we never wrote\n"))

    def test_the_frozen_hash_manifest_is_well_formed(self):
        manifest = json.loads((ROOT / "payload" / "legacy-copy-hashes.json").read_text())
        released = manifest["released"]
        for key in ("first_commit", "through_commit"):
            self.assertRegex(released[key], r"^[0-9a-f]{40}$")
        hashes = released["sha256"]
        self.assertEqual(hashes, sorted(set(hashes)))
        self.assertTrue(all(len(h) == 64 and int(h, 16) >= 0 for h in hashes))
        for fixture in FIXTURES.rglob("*.*"):
            with self.subTest(fixture=fixture.name):
                self.assertIn(hashlib.sha256(fixture.read_bytes()).hexdigest(), hashes)


class TestUpgradeFromRelease(unittest.TestCase):
    """A Codex install a real release left behind, upgraded by this checkout's CLI.

    tests/fixtures/installs/codex-12.2026100906.0 is ~/.codex and
    ~/.leos-agent-local as that release's own installer wrote them. The upgrade
    runs as a user would run it, `python3 scripts/leo-install.py codex`, with
    only HOME pointing at the seeded tree.
    """

    RELEASE = Path(__file__).resolve().parent / "fixtures" / "installs" / "codex-12.2026100906.0"
    RETIRED = ("leo-runner", "leo-executor")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()

    def seed(self, name):
        home = self.base / name
        shutil.copytree(self.RELEASE / "codex", home / ".codex")
        shutil.copytree(self.RELEASE / "leos-agent-local", home / ".leos-agent-local")
        backup = home / ".leos-agent-local" / "install-backups" / "codex.json"
        backup.write_text(backup.read_text().replace("{home}", str(home)))
        return home

    def install(self, home, *extra):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
               "LEOS_AGENT_PRICE_REFRESH": "off", "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "leo-install.py"), "codex", *extra],
                              env=env, capture_output=True, text=True, timeout=120)

    def payload_agents(self):
        return sorted(p.name for p in (ROOT / "payload" / "codex-agents").glob("*.toml"))

    def test_an_upgrade_replaces_owned_profiles_and_keeps_the_users_files(self):
        home = self.seed("upgraded")
        codex = home / ".codex"
        mine = {"agents/my-helper.toml": b'name = "my-helper"\ndescription = "mine"\n',
                "AGENTS.md": b"# my notes\n"}
        for rel, data in mine.items():
            (codex / rel).write_bytes(data)
        config = (codex / "config.toml").read_bytes()
        old_receipt = json.loads((codex / "leos-agent-paths.json").read_text())["files"]
        # Copies from releases before receipts, still on disk.
        for name in self.RETIRED:
            shutil.copy(FIXTURES / "codex" / "agents" / f"{name}.toml", codex / "agents")

        done = self.install(home)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        agents = sorted(p.name for p in (codex / "agents").glob("leo-*.toml"))
        self.assertEqual(agents, self.payload_agents())
        self.assertIn("leo-lens.toml", agents)  # added after that release
        for name in self.RETIRED:
            self.assertFalse((codex / "agents" / f"{name}.toml").exists(), name)
        for rel, data in mine.items():
            self.assertEqual((codex / rel).read_bytes(), data, rel)
        self.assertEqual((codex / "config.toml").read_bytes(), config)

        # The receipt names exactly the profiles now on disk, by their new bytes.
        receipt = json.loads((codex / "leos-agent-paths.json").read_text())["files"]
        self.assertEqual(receipt, {f"agents/{name}": hashlib.sha256((codex / "agents" / name).read_bytes()).hexdigest()
                                   for name in agents})
        self.assertNotEqual(receipt, old_receipt)
        # Same bytes as a fresh install of this checkout.
        fresh = self.base / "fresh"
        (fresh / ".codex").mkdir(parents=True)
        self.assertEqual(self.install(fresh).returncode, 0)
        for name in agents:
            self.assertEqual((codex / "agents" / name).read_bytes(),
                             (fresh / ".codex" / "agents" / name).read_bytes(), name)
        # The rollback record now describes this install, under this home.
        backup = json.loads((home / ".leos-agent-local" / "install-backups" / "codex.json").read_text())
        recorded = {Path(entry["path"]).name for entry in backup["files"]}
        self.assertLessEqual({"leo-lens.toml", "leo-cheap.toml"}, recorded)
        self.assertTrue(all(entry["path"].startswith(str(home)) for entry in backup["files"]))

        # A second run changes nothing.
        before = tree(home, skip=())
        again = self.install(home)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(tree(home, skip=()), before)

    def test_an_edited_profile_stops_the_upgrade_before_anything_is_written(self):
        home = self.seed("edited")
        cheap = home / ".codex" / "agents" / "leo-cheap.toml"
        cheap.write_text(cheap.read_text() + "# my tweak\n")
        before = tree(home, skip=())
        done = self.install(home)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("conflict", done.stdout)
        self.assertEqual(tree(home, skip=()), before)


if __name__ == "__main__":
    unittest.main()
