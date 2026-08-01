"""scripts/memory.py: the canonical memory store and its one-way projection
into per-user harness memory surfaces. Stdlib unittest only.

Every case redirects LEOS_AGENT_LOCAL_PATH and all four harness home variables
into a temp dir. A case that forgets to would rewrite the developer's real
~/.claude/CLAUDE.md, which is precisely what this subsystem must never do.

Run: python3 -m unittest tests.test_memory -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_PY = os.path.join(REPO, "plugins", "leo", "scripts", "memory.py")


def _load():
    spec = importlib.util.spec_from_file_location("leo_memory", MEMORY_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class MemoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.env = {
            "LEOS_AGENT_LOCAL_PATH": os.path.join(base, "local"),
            "CLAUDE_CONFIG_DIR": os.path.join(base, "claude"),
            "CODEX_HOME": os.path.join(base, "codex"),
            "XDG_CONFIG_HOME": os.path.join(base, "xdg"),
            "HOME": os.path.join(base, "home"),
            "HERMES_HOME": os.path.join(base, "hermes"),
        }
        self._saved = {k: os.environ.get(k) for k in self.env}
        self._saved["LEOS_AGENT_NO_PROJECT"] = os.environ.get("LEOS_AGENT_NO_PROJECT")
        os.environ.pop("LEOS_AGENT_NO_PROJECT", None)
        os.environ.update(self.env)
        # Gate dirs: present ones receive projection, absent ones must not.
        os.makedirs(self.env["CLAUDE_CONFIG_DIR"], exist_ok=True)
        os.makedirs(self.env["CODEX_HOME"], exist_ok=True)
        self.memory = _load()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write(self, scope="global", type_="preference", title="A fact",
              body="The body of the fact.", repo=None):
        return self.memory.write_fact(scope, type_, title, body, repo)

    def cli(self, *args, stdin=""):
        env = dict(os.environ)
        env.update(self.env)
        return subprocess.run(
            [sys.executable, MEMORY_PY, *args],
            env=env, input=stdin, capture_output=True, text=True, timeout=30,
        )

    @property
    def codex_file(self):
        return os.path.join(self.env["CODEX_HOME"], "AGENTS.md")


class TestSlugs(MemoryCase):
    def test_repo_slug_separates_keys_that_normalize_alike(self):
        slugs = {self.memory.repo_slug(k)
                 for k in ("owner/repo", "owner-repo", "Owner/Repo")}
        self.assertEqual(len(slugs), 3, "keys that differ must not share a directory")

    def test_repo_slug_shape_is_filesystem_safe(self):
        for key in ("owner/repo", "/abs/path/to/project", "Weird Name!/x"):
            slug = self.memory.repo_slug(key)
            self.assertRegex(slug, r"^[a-z0-9._-]{1,60}--[0-9a-f]{8}$")
            self.assertFalse(slug.startswith("-"))

    def test_repo_slug_is_stable(self):
        self.assertEqual(self.memory.repo_slug("owner/repo"),
                         self.memory.repo_slug("owner/repo"))

    def test_fact_slug_strips_punctuation_and_never_empties(self):
        self.assertEqual(self.memory.fact_slug("Squash-merge: preference!"),
                         "squash-merge-preference")
        self.assertEqual(self.memory.fact_slug("???"), "fact")


class TestWriteAndRead(MemoryCase):
    def test_roundtrip_keeps_body_and_frontmatter(self):
        result = self.write(title="Squash merge", body="Leo squashes, always.")
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["ref"], "global/squash-merge")
        meta, body = self.memory.parse_fact(result["path"])
        self.assertEqual(body, "Leo squashes, always.")
        self.assertEqual(meta["title"], "Squash merge")
        self.assertEqual(meta["type"], "preference")
        self.assertEqual(meta["scope"], "global")

    def test_same_title_and_type_updates_in_place(self):
        first = self.write(title="T", body="original")
        created = self.memory.parse_fact(first["path"])[0]["created"]
        second = self.write(title="T", body="replacement")
        self.assertEqual(second["action"], "updated")
        self.assertEqual(second["path"], first["path"])
        meta, body = self.memory.parse_fact(second["path"])
        self.assertEqual(body, "replacement")
        self.assertEqual(meta["created"], created, "created must survive an update")
        directory = os.path.dirname(first["path"])
        self.assertEqual(len([n for n in os.listdir(directory) if n.endswith(".md")]), 1)

    def test_same_title_different_type_gets_its_own_file(self):
        first = self.write(title="T", type_="preference")
        second = self.write(title="T", type_="convention")
        self.assertNotEqual(first["path"], second["path"])

    def test_repo_scope_requires_a_key(self):
        with self.assertRaises(SystemExit):
            self.write(scope="repo")

    def test_rejects_unknown_type_and_oversized_body(self):
        with self.assertRaises(SystemExit):
            self.write(type_="anecdote")
        with self.assertRaises(SystemExit):
            self.write(body="x" * (self.memory.MAX_BODY + 1))

    def test_cli_write_does_not_absorb_flag_values_into_the_title(self):
        done = self.cli("write", "repo", "convention", "Generated adapters",
                        "--repo", "owner/repo", stdin="body text")
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        meta = self.memory.parse_fact(payload["path"])[0]
        self.assertEqual(meta["title"], "Generated adapters")
        self.assertEqual(meta["repo"], "owner/repo")


class TestForget(MemoryCase):
    def test_forget_trashes_rather_than_deletes(self):
        written = self.write(title="Gone soon")
        result = self.memory.forget(written["ref"])
        self.assertFalse(os.path.exists(written["path"]))
        self.assertTrue(os.path.exists(result["path"]))
        self.assertIn(".trash", result["path"])

    def test_forgetting_an_absent_ref_fails(self):
        with self.assertRaises(SystemExit):
            self.memory.forget("global/never-existed")

    def test_invalid_ref_is_rejected(self):
        for bad in ("../escape", "/etc/passwd", "global"):
            with self.assertRaises(SystemExit):
                self.memory.ref_path(bad)


class TestIndex(MemoryCase):
    def test_index_is_derived_and_rebuildable(self):
        self.write(title="One")
        index_path = os.path.join(self.memory.memory_root(), "index.json")
        os.unlink(index_path)
        rebuilt = self.memory.reindex()
        self.assertEqual(len(rebuilt["facts"]), 1)
        self.assertTrue(os.path.exists(index_path))

    def test_corrupt_index_does_not_raise_on_the_read_path(self):
        self.write(title="One")
        with open(os.path.join(self.memory.memory_root(), "index.json"), "w") as fh:
            fh.write("{ not json")
        self.assertIsNone(self.memory._load_index())
        block = self.memory.render_context(self.memory._load_index() or self.memory.reindex())
        self.assertIn("One", block)

    def test_unreadable_fact_is_skipped_not_deleted(self):
        self.write(title="Good")
        stray = os.path.join(self.memory.memory_root(), "global", "broken.md")
        with open(stray, "w") as fh:
            fh.write("no frontmatter here")
        index = self.memory.reindex()
        self.assertEqual([e["title"] for e in index["facts"]], ["Good"])
        self.assertIn("global/broken", index["unreadable"])
        self.assertTrue(os.path.exists(stray), "a corrupt fact must never be deleted")

    def test_context_is_bounded_and_carries_no_placeholder(self):
        for n in range(120):
            self.write(title=f"Fact number {n}", body=f"Body {n}")
        block = self.memory.render_context(self.memory.reindex())
        self.assertLessEqual(len(block), self.memory.MEMORY_CONTEXT_LIMIT)
        self.assertIn("and", block)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", block)

    def test_context_includes_only_the_current_repo(self):
        self.write(scope="repo", title="Mine", repo="owner/mine")
        self.write(scope="repo", title="Theirs", repo="owner/theirs")
        block = self.memory.render_context(self.memory.reindex(), repo="owner/mine")
        self.assertIn("Mine", block)
        self.assertNotIn("Theirs", block)


class TestProjection(MemoryCase):
    def test_creates_block_in_existing_gate_dir_only(self):
        self.write(title="Global fact")
        self.assertTrue(os.path.exists(self.codex_file))
        statuses = {t["harness"]: t["status"] for t in self.memory.project()}
        self.assertEqual(statuses["opencode"], "skipped:no-dir")
        self.assertEqual(statuses["cursor"], "skipped:no-dir")
        self.assertEqual(statuses["hermes"], "skipped:opt-in-required")
        self.assertFalse(os.path.exists(os.path.join(self.env["XDG_CONFIG_HOME"], "opencode")),
                         "projection must never create a harness config dir")

    def test_hermes_is_reported_even_when_not_enabled(self):
        """A harness silently missing from the report reads as one that was
        projected."""
        self.write(title="Global fact")
        harnesses = {t["harness"] for t in self.memory.project()}
        self.assertIn("hermes", harnesses)

    def test_user_content_is_preserved_byte_for_byte(self):
        with open(self.codex_file, "w") as fh:
            fh.write("# My notes\nkeep me\n")
        self.write(title="Global fact")
        text = _read(self.codex_file)
        self.assertTrue(text.startswith("# My notes\nkeep me\n"))
        self.assertIn(self.memory.BEGIN, text)

    def test_backup_taken_once_and_matches_the_original(self):
        original = "# My notes\nkeep me\n"
        with open(self.codex_file, "w") as fh:
            fh.write(original)
        self.write(title="First")
        backup = self.codex_file + ".leo-backup"
        self.assertEqual(_read(backup), original)
        self.write(title="Second")
        self.assertEqual(_read(backup), original,
                         "the backup must capture the pre-Leo original, not a later state")

    def test_reprojection_is_idempotent(self):
        self.write(title="Global fact")
        first = _read(self.codex_file)
        statuses = {t["harness"]: t["status"] for t in self.memory.project()}
        self.assertEqual(statuses["codex"], "unchanged")
        self.assertEqual(_read(self.codex_file), first)
        self.assertEqual(first.count(self.memory.BEGIN), 1)
        self.assertEqual(first.count(self.memory.END), 1)

    def test_unbalanced_markers_leave_the_file_untouched(self):
        broken = "mine\n" + self.memory.BEGIN + "\nstray\n"
        with open(self.codex_file, "w") as fh:
            fh.write(broken)
        statuses = {t["harness"]: t["status"] for t in self.memory.project()}
        self.assertEqual(statuses["codex"], "error: unbalanced markers")
        self.assertEqual(_read(self.codex_file), broken)

    def test_repo_facts_are_never_projected(self):
        self.write(scope="repo", title="Repo only", repo="owner/repo")
        self.assertFalse(os.path.exists(self.codex_file),
                         "a per-user surface loads everywhere; repo facts must stay out")

    def test_emptying_the_store_removes_the_block(self):
        written = self.write(title="Temporary")
        self.assertIn(self.memory.BEGIN, _read(self.codex_file))
        self.memory.forget(written["ref"])
        text = _read(self.codex_file) if os.path.exists(self.codex_file) else ""
        self.assertNotIn(self.memory.BEGIN, text)

    def test_existing_mode_is_preserved(self):
        with open(self.codex_file, "w") as fh:
            fh.write("mine\n")
        os.chmod(self.codex_file, 0o644)
        self.write(title="Global fact")
        self.assertEqual(os.stat(self.codex_file).st_mode & 0o777, 0o644)

    def test_kill_switch_writes_nothing(self):
        os.environ["LEOS_AGENT_NO_PROJECT"] = "1"
        self.write(title="Global fact")
        self.assertFalse(os.path.exists(self.codex_file))
        self.assertTrue(all(t["status"].startswith("skipped")
                            for t in self.memory.project()))

    def test_never_writes_into_a_git_repo(self):
        repo_dir = os.path.join(self.tmp.name, "project")
        os.makedirs(repo_dir)
        subprocess.run(["git", "init", "-q", repo_dir], check=True,
                       capture_output=True)
        for name in ("CLAUDE.md", "AGENTS.md"):
            with open(os.path.join(repo_dir, name), "w") as fh:
                fh.write("project file\n")
        before = {n: _read(os.path.join(repo_dir, n))
                  for n in ("CLAUDE.md", "AGENTS.md")}
        cwd = os.getcwd()
        os.chdir(repo_dir)
        try:
            self.write(title="Global fact")
            self.memory.project()
        finally:
            os.chdir(cwd)
        for name, text in before.items():
            self.assertEqual(_read(os.path.join(repo_dir, name)), text)


class TestHermesProjection(MemoryCase):
    """Hermes is opt-in and its file is never created.

    SOUL.md is the agent's identity prompt and the opening section of every
    Hermes system prompt on the machine, and Hermes substitutes a built-in
    persona when it is absent — so creating it would silently replace who the
    user's agent is. Every other target gates on its directory; this one gates
    on the file too.
    """

    @property
    def soul(self):
        return os.path.join(self.env["HERMES_HOME"], "SOUL.md")

    def enable(self):
        path = self.memory.state.state_file(self.memory.SETUP_STATE)
        self.memory.state.atomic_write(path, {"hermes": {"projectMemory": True}})

    def status(self):
        self.write(title="Global fact")
        return {t["harness"]: t["status"] for t in self.memory.project()}

    def test_disabled_by_default(self):
        self.assertFalse(self.memory.hermes_enabled())
        self.assertEqual(self.status()["hermes"], "skipped:opt-in-required")

    def test_enabled_without_hermes_home_writes_nothing(self):
        self.enable()
        self.assertEqual(self.status()["hermes"], "skipped:no-dir")
        self.assertFalse(os.path.exists(self.env["HERMES_HOME"]))

    def test_soul_is_never_created(self):
        """The assertion that protects the user's agent identity."""
        self.enable()
        os.makedirs(self.env["HERMES_HOME"], exist_ok=True)
        self.assertEqual(self.status()["hermes"], "skipped:no-soul")
        self.assertFalse(os.path.exists(self.soul))

    def test_existing_persona_is_preserved_and_backed_up(self):
        original = "You are a terse assistant.\n"
        self.enable()
        os.makedirs(self.env["HERMES_HOME"], exist_ok=True)
        with open(self.soul, "w") as fh:
            fh.write(original)
        self.assertNotIn("skipped", self.status()["hermes"])
        text = _read(self.soul)
        self.assertTrue(text.startswith(original))
        self.assertIn(self.memory.BEGIN, text)
        self.assertEqual(_read(self.soul + ".leo-backup"), original)

    def test_reprojection_is_idempotent(self):
        self.enable()
        os.makedirs(self.env["HERMES_HOME"], exist_ok=True)
        with open(self.soul, "w") as fh:
            fh.write("You are a terse assistant.\n")
        self.status()
        first = _read(self.soul)
        statuses = {t["harness"]: t["status"] for t in self.memory.project()}
        self.assertEqual(statuses["hermes"], "unchanged")
        self.assertEqual(_read(self.soul), first)
        self.assertEqual(first.count(self.memory.BEGIN), 1)

    def test_repo_facts_never_reach_the_persona_file(self):
        self.enable()
        os.makedirs(self.env["HERMES_HOME"], exist_ok=True)
        with open(self.soul, "w") as fh:
            fh.write("You are a terse assistant.\n")
        self.write(scope="repo", title="Repo secret sauce", repo="owner/mine")
        self.write(title="Global fact")
        self.memory.project()
        text = _read(self.soul)
        self.assertIn("Global fact", text)
        self.assertNotIn("Repo secret sauce", text)

    def test_agent_owned_memory_files_are_never_touched(self):
        """MEMORY.md and USER.md belong to Hermes' own memory tool, which
        would overwrite Leo's markers."""
        self.enable()
        memories = os.path.join(self.env["HERMES_HOME"], "memories")
        os.makedirs(memories, exist_ok=True)
        for name in ("MEMORY.md", "USER.md"):
            with open(os.path.join(memories, name), "w") as fh:
                fh.write(f"agent-owned {name}\n")
        with open(self.soul, "w") as fh:
            fh.write("You are a terse assistant.\n")
        self.status()
        for name in ("MEMORY.md", "USER.md"):
            self.assertEqual(_read(os.path.join(memories, name)), f"agent-owned {name}\n")

    def test_global_no_project_still_wins(self):
        self.enable()
        os.makedirs(self.env["HERMES_HOME"], exist_ok=True)
        with open(self.soul, "w") as fh:
            fh.write("You are a terse assistant.\n")
        os.environ["LEOS_AGENT_NO_PROJECT"] = "1"
        self.addCleanup(os.environ.pop, "LEOS_AGENT_NO_PROJECT", None)
        self.write(title="Global fact")
        statuses = {t["harness"]: t["status"] for t in self.memory.project()}
        self.assertEqual(statuses["hermes"], "skipped:disabled")
        self.assertNotIn(self.memory.BEGIN, _read(self.soul))


class TestDataRoot(MemoryCase):
    def test_honours_the_override_and_ignores_the_pre_6_variable(self):
        self.assertTrue(self.memory.memory_root().startswith(
            self.env["LEOS_AGENT_LOCAL_PATH"]))
        os.environ["LEOS_AGENT_PATH"] = os.path.join(self.tmp.name, "legacy")
        self.addCleanup(os.environ.pop, "LEOS_AGENT_PATH", None)
        self.assertNotIn("legacy", self.memory.memory_root())

    def test_store_name_cannot_collide_with_flat_state_files(self):
        self.write(title="One")
        self.assertTrue(os.path.isdir(self.memory.memory_root()))
        self.assertFalse(os.path.exists(
            os.path.join(self.env["LEOS_AGENT_LOCAL_PATH"], "memory.json")))


class TestConcurrency(MemoryCase):
    def test_parallel_writes_all_survive(self):
        errors = []

        def writer(n):
            try:
                self.write(title=f"Parallel fact {n}", body=f"body {n}")
            except BaseException as exc:  # pragma: no cover - failure detail
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        index = self.memory.reindex()
        self.assertEqual(len(index["facts"]), 20)


class TestCli(MemoryCase):
    def test_context_and_session_never_fail(self):
        for verb in ("context", "session"):
            done = self.cli(verb)
            self.assertEqual(done.returncode, 0, f"{verb}: {done.stderr}")

    def test_read_prints_the_file_verbatim(self):
        written = self.write(title="Readable", body="the body")
        done = self.cli("read", written["ref"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("the body", done.stdout)
        self.assertIn('title: "Readable"', done.stdout)

    def test_bad_verb_exits_non_zero(self):
        self.assertNotEqual(self.cli("frobnicate").returncode, 0)


if __name__ == "__main__":
    unittest.main()
