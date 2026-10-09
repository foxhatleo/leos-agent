"""Behavioral tests for the npm publish gate.

This path runs once per tag and never in ordinary development, so the decisions
it makes — publish, skip, or refuse — are tested directly rather than exercised.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("publish_npm_test", ROOT / "scripts" / "publish-npm.py")
publish_npm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish_npm)


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["npm"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestPackGuard(unittest.TestCase):
    def test_build_residue_is_refused(self):
        inventory = ["LICENSE", "package.json", "scripts/check.py", "scripts/__pycache__/check.cpython-312.pyc"]
        self.assertEqual(
            publish_npm.forbidden_paths(inventory),
            ["scripts/__pycache__/check.cpython-312.pyc"],
        )
        with self.assertRaises(publish_npm.ReleaseError):
            publish_npm.check_inventory(inventory)

    def test_a_clean_tree_passes(self):
        publish_npm.check_inventory(sorted(publish_npm.required_files()))

    def test_missing_native_profile_breaks_install_and_is_refused(self):
        with self.assertRaisesRegex(publish_npm.ReleaseError, "agents/leo-standard.md"):
            publish_npm.check_inventory(publish_npm.required_files() - {"agents/leo-standard.md"})

    def test_a_pack_missing_a_script_the_adapters_run_is_refused(self):
        # The Pi and OpenCode adapters start these, and every adapter fails
        # open, so an install without one loses its policy or its completion
        # rows without a single error anyone would see.
        inventory = set(publish_npm.pack_inventory())
        for rel in ("scripts/emit_payload.py", "scripts/observe_agent.py", "scripts/outcome.py",
                    "scripts/dispatch_guard.py", "scripts/harness_bridge.js"):
            with self.subTest(missing=rel):
                with self.assertRaisesRegex(publish_npm.ReleaseError, rel):
                    publish_npm.check_inventory(inventory - {rel})

    def test_missing_license_is_refused(self):
        with self.assertRaises(publish_npm.ReleaseError):
            publish_npm.check_inventory(["package.json", "index.js"])

    def test_the_real_package_tree_is_clean(self):
        # The declared `files` allowlist must exclude residue on its own, since
        # CI runs the test suite — which writes __pycache__ — before publishing.
        publish_npm.check_inventory(publish_npm.pack_inventory())

    def test_packaged_opencode_installer_round_trip(self):
        inventory = publish_npm.pack_inventory()
        with tempfile.TemporaryDirectory(prefix="leo packaged install ") as tmp:
            base = Path(tmp)
            package = base / "package"
            for name in inventory:
                target = package / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            env = {**os.environ, "LEOS_AGENT_ROOT": str(package),
                   "OPENCODE_CONFIG_DIR": str(base / "config"),
                   "OPENCODE_CONFIG": str(base / "config" / "opencode.jsonc"),
                   "LEOS_AGENT_LOCAL_PATH": str(base / "data"),
                   "LEOS_AGENT_PRICE_REFRESH": "off", "PYTHONDONTWRITEBYTECODE": "1"}
            command = [sys.executable, str(package / "scripts/leo-install.py"), "opencode"]
            for flags in ([], ["--check"], ["--uninstall"]):
                result = subprocess.run(command + flags, env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((base / "config" / "agents" / "leo-standard.md").exists())


class TestRegistryState(unittest.TestCase):
    def _with_view(self, result):
        original = publish_npm.run
        publish_npm.run = lambda command: result
        self.addCleanup(lambda: setattr(publish_npm, "run", original))

    def test_exact_version_present_is_a_noop(self):
        self._with_view(completed(0, stdout="10.1.0\n"))
        self.assertEqual(publish_npm.registry_state("10.1.0"), "present")

    def test_confirmed_404_allows_publishing(self):
        self._with_view(completed(1, stderr="npm error code E404\nnpm error 404 Not Found"))
        self.assertEqual(publish_npm.registry_state("10.1.0"), "absent")

    def test_an_ambiguous_failure_refuses_rather_than_publishing(self):
        # An auth failure or registry outage must never be read as "absent".
        self._with_view(completed(1, stderr="npm error code E401\nnpm error Unauthorized"))
        with self.assertRaises(publish_npm.ReleaseError):
            publish_npm.registry_state("10.1.0")

    def test_a_mismatched_lookup_refuses(self):
        self._with_view(completed(0, stdout="9.9.9\n"))
        with self.assertRaises(publish_npm.ReleaseError):
            publish_npm.registry_state("10.1.0")


class TestTagAgreement(unittest.TestCase):
    def test_an_accepted_upload_the_registry_has_not_served_yet_is_not_a_failure(self):
        """Once npm accepts the upload the release has happened. Reporting the
        registry's read lag as a failure marked three consecutive successful
        releases red, which is how a release signal stops being read."""
        with mock.patch.object(publish_npm, "pack_inventory", return_value=publish_npm.required_files()), \
             mock.patch.object(publish_npm, "newest_release", return_value=None), \
             mock.patch.object(publish_npm, "registry_state", return_value="absent"), \
             mock.patch.object(publish_npm, "publish") as upload, \
             mock.patch.object(publish_npm.time, "sleep"), \
             contextlib.redirect_stderr(io.StringIO()) as err, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(publish_npm.main([]), 0)
        upload.assert_called_once()
        self.assertIn("published leos-agent@", out.getvalue())
        # It must not keep asserting a staging hold: propagation is what the
        # three red releases actually turned out to be.
        self.assertNotIn("staged packages", err.getvalue())
        self.assertIn("npm view", err.getvalue())

    def test_a_lookup_outage_after_publishing_is_reported_but_never_fails(self):
        with mock.patch.object(publish_npm, "pack_inventory", return_value=publish_npm.required_files()), \
             mock.patch.object(publish_npm, "newest_release", return_value=None), \
             mock.patch.object(publish_npm, "registry_state",
                               side_effect=["absent", publish_npm.ReleaseError("auth")]), \
             mock.patch.object(publish_npm, "publish") as upload, \
             mock.patch.object(publish_npm.time, "sleep"), \
             contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(publish_npm.main([]), 0)
        upload.assert_called_once()
        self.assertIn("registry lookup failed", err.getvalue())

    def test_a_failed_upload_still_fails(self):
        """The one thing that must stay fatal."""
        with mock.patch.object(publish_npm, "pack_inventory", return_value=publish_npm.required_files()), \
             mock.patch.object(publish_npm, "newest_release", return_value=None), \
             mock.patch.object(publish_npm, "registry_state", return_value="absent"), \
             mock.patch.object(publish_npm, "publish",
                               side_effect=publish_npm.ReleaseError("npm publish failed: 402")), \
             contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(publish_npm.main([]), 1)
        self.assertIn("npm publish failed", err.getvalue())

    def test_registry_propagation_does_not_retry_the_upload(self):
        with mock.patch.object(publish_npm, "pack_inventory", return_value=publish_npm.required_files()), \
             mock.patch.object(publish_npm, "newest_release", return_value=None), \
             mock.patch.object(publish_npm, "registry_state", side_effect=["absent", "absent", "present"]), \
             mock.patch.object(publish_npm, "publish") as upload, \
             mock.patch.object(publish_npm.time, "sleep") as sleep, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(publish_npm.main([]), 0)
        upload.assert_called_once()
        sleep.assert_called_once_with(5)

    def test_verification_errors_are_not_reinterpreted_as_propagation(self):
        with mock.patch.object(publish_npm, "registry_state", side_effect=publish_npm.ReleaseError("auth")), \
             mock.patch.object(publish_npm.time, "sleep") as sleep:
            with self.assertRaises(publish_npm.ReleaseError):
                publish_npm.wait_for_public_version("12.2026090802.0")
        sleep.assert_not_called()

    def test_declared_version_matches_package_json(self):
        expected = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        self.assertEqual(publish_npm.declared_version(), expected)

    def test_a_tag_that_disagrees_with_package_json_aborts(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = publish_npm.main(["--tag", "v0.0.1", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("does not match package.json", stderr.getvalue())


def version_order(version):
    return tuple(int(part) for part in version.split("."))


class FakeNpm:
    """npm's command line as the publish script drives it, over an in-memory registry.

    Output shapes follow the real CLI: `npm view <pkg>@<v> version` prints the
    bare version, several fields with --json print one JSON object, and a
    missing package or version fails with E404. `npm publish` applies `latest`
    unless given --tag. npm 11, which the release workflow installs, refuses to
    apply it implicitly once a higher version is published; npm 10 applies it
    anyway, moving `latest` backwards.
    """

    E404 = "npm error code E404\nnpm error 404 Not Found - GET https://registry.npmjs.org/leos-agent - Not found\n"

    def __init__(self, versions=(), latest=None, refuses_implicit_latest=True, listing_error=None):
        self.versions = list(versions)
        self.dist_tags = {"latest": latest} if latest else {}
        self.refuses_implicit_latest = refuses_implicit_latest
        self.listing_error = listing_error
        self.published = []

    def __call__(self, command):
        args = command[1:]
        if args[:3] == ["pack", "--dry-run", "--json"]:
            files = [{"path": path} for path in sorted(publish_npm.required_files())]
            return completed(0, stdout=json.dumps([{"name": "leos-agent", "files": files}]))
        if args[:1] == ["view"] and args[2:] == ["version"] and args[1].startswith("leos-agent@"):
            version = args[1].split("@", 1)[1]
            return completed(0, stdout=version + "\n") if version in self.versions else completed(1, stderr=self.E404)
        if args == ["view", "leos-agent", "dist-tags", "versions", "--json"]:
            if self.listing_error:
                return completed(1, stderr=self.listing_error)
            if not self.versions:
                return completed(1, stdout=json.dumps({"error": {"code": "E404"}}), stderr=self.E404)
            report = {"dist-tags": dict(self.dist_tags), "versions": sorted(self.versions, key=version_order)}
            return completed(0, stdout=json.dumps(report, indent=2) + "\n")
        if args[:3] == ["publish", "--access", "public"]:
            tag = args[args.index("--tag") + 1] if "--tag" in args else None
            version = publish_npm.declared_version()
            if version in self.versions:
                return completed(1, stderr="npm error You cannot publish over the previously published versions")
            highest = max(self.versions, key=version_order, default=None)
            if self.refuses_implicit_latest and tag is None and highest and version_order(highest) >= version_order(version):
                return completed(1, stderr=f'npm error Cannot implicitly apply the "latest" tag because previously '
                                           f"published version {highest} is higher than the new version {version}. "
                                           "You must specify a tag using --tag.")
            self.versions.append(version)
            self.dist_tags[tag or "latest"] = version
            self.published.append((version, tag))
            return completed(0, stdout=f"+ leos-agent@{version}\n")
        raise AssertionError(f"FakeNpm does not model {command!r}")


class TestLatestOnlyMovesForward(unittest.TestCase):
    def setUp(self):
        self.version = publish_npm.declared_version()
        self.newer = f"{int(self.version.split('.')[0]) + 1}.0.0"
        self.older = "1.0.0"

    def release(self, npm, *argv):
        with mock.patch.object(publish_npm, "run", npm), mock.patch.object(publish_npm.time, "sleep"), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = publish_npm.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_an_old_tag_rerun_after_a_newer_release_leaves_latest_alone(self):
        # On npm 10 a plain publish would move latest back to this version; on
        # npm 11 it would refuse, and this tag could never publish at all.
        for refuses, npm_major in ((True, "11"), (False, "10")):
            with self.subTest(npm=npm_major):
                npm = FakeNpm([self.older, self.newer], latest=self.newer, refuses_implicit_latest=refuses)
                code, output = self.release(npm)
                self.assertEqual(code, 0, output)
                self.assertEqual(npm.published, [(self.version, publish_npm.BACKFILL_TAG)])
                self.assertEqual(npm.dist_tags["latest"], self.newer)

    def test_a_newer_version_already_published_counts_even_with_latest_moved_back(self):
        # latest can be pointed at an older release by hand; npm 11's guard still
        # compares against the highest version, so the publish must too.
        npm = FakeNpm([self.older, self.newer], latest=self.older)
        code, output = self.release(npm)
        self.assertEqual(code, 0, output)
        self.assertEqual(npm.published, [(self.version, publish_npm.BACKFILL_TAG)])
        self.assertEqual(npm.dist_tags["latest"], self.older)

    def test_the_newest_release_takes_latest(self):
        npm = FakeNpm([self.older], latest=self.older)
        code, output = self.release(npm)
        self.assertEqual(code, 0, output)
        self.assertEqual(npm.published, [(self.version, None)])
        self.assertEqual(npm.dist_tags, {"latest": self.version})

    def test_the_first_publish_takes_latest(self):
        npm = FakeNpm()
        code, output = self.release(npm)
        self.assertEqual(code, 0, output)
        self.assertEqual(npm.dist_tags, {"latest": self.version})

    def test_a_version_already_published_is_still_never_republished(self):
        npm = FakeNpm([self.version, self.newer], latest=self.newer)
        code, output = self.release(npm)
        self.assertEqual(code, 0, output)
        self.assertEqual(npm.published, [])

    def test_a_dry_run_names_the_tag_and_publishes_nothing(self):
        npm = FakeNpm([self.newer], latest=self.newer)
        code, output = self.release(npm, "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"under dist-tag {publish_npm.BACKFILL_TAG}", output)
        self.assertEqual(npm.published, [])

    def test_an_ambiguous_tag_lookup_publishes_nothing(self):
        npm = FakeNpm([self.older], latest=self.older, listing_error="npm error code E401\nnpm error Unauthorized")
        code, output = self.release(npm)
        self.assertEqual(code, 1, output)
        self.assertIn("dist-tag lookup failed", output)
        self.assertEqual(npm.published, [])


# Runs a script the way harness_bridge.js does, under an audit hook that records
# every file below the package root it opens or imports -- including a module a
# fail-open adapter would otherwise lose without a trace.
AUDIT = r"""
import atexit, json, os, runpy, sys
root, log, script = os.path.realpath(sys.argv[1]), sys.argv[2], sys.argv[3]
scripts = os.path.join(root, "scripts")
touched = set()

def note(path):
    try:
        path = os.path.realpath(os.fspath(path))
    except TypeError:
        return
    if path.startswith(root + os.sep) and "__pycache__" not in path:
        touched.add(os.path.relpath(path, root).replace(os.sep, "/"))

def hook(event, args):
    if event == "open" and args and not isinstance(args[0], int):
        note(args[0])
    elif event == "import" and args:
        name = str(args[0]).partition(".")[0]
        if os.path.isfile(os.path.join(scripts, name + ".py")):
            touched.add("scripts/" + name + ".py")
    elif event == "subprocess.Popen" and len(args) > 1:
        for arg in args[1] or ():
            if isinstance(arg, str):
                note(arg)

def dump():
    with open(log, "w") as handle:
        handle.write(json.dumps(sorted(touched)))

atexit.register(dump)
sys.addaudithook(hook)
sys.argv = [script, *sys.argv[4:]]
runpy.run_path(script, run_name="__main__")
"""


class TestTheRequiredSetCoversWhatAdaptersRun(unittest.TestCase):
    # The calls index.js (OpenCode) and pi-extension.js (Pi) make, with the
    # events they send. pricing.py refresh is left out: it is network-only.
    CALLS = (
        ("opencode", "dispatch_guard.py", ["--json"], {
            "tool_name": "task", "session_id": "s-1", "call_id": "c-1", "parent_model": "example/parent",
            "tool_input": {"subagent_type": "leo-cheap", "description": "d", "prompt": "Escalation from cheap: retry"},
            "native_profiles": {"leo-cheap": {"model": "example/cheap"}},
        }),
        ("opencode", "observe_agent.py", [], {
            "hook_event_name": "SubagentStop", "tool_name": "task", "session_id": "s-1", "call_id": "c-1",
            "agent": "leo-cheap", "result_text": "Result: done\nVerified: ran the tests",
            "outcome_source": "tool-output", "reason": "opencode-tool-output",
        }),
        ("pi", "emit_payload.py", [], {"hook_event_name": "SessionStart"}),
        ("pi", "dispatch_guard.py", ["--json"], {
            "tool_name": "subagent", "toolCallId": "t-1", "parent_model": "example-parent",
            "tool_input": {"agent": "leo-cheap", "task": "Escalation from cheap: retry"},
        }),
        ("pi", "observe_agent.py", [], {
            "hook_event_name": "SubagentStop", "tool_name": "subagent", "toolCallId": "t-1", "agent": "leo-cheap",
            "result_text": "Result: done\nVerified: ran the tests", "status": None, "usage": None,
            "reason": "pi-tool-result",
        }),
    )

    def test_every_file_an_adapter_call_touches_is_required(self):
        with tempfile.TemporaryDirectory(prefix="leo adapter closure ") as tmp:
            base = Path(tmp)
            # The adapters pass the root only as the script's path; the scripts
            # find it from there, as they do in a real install.
            env = {key: value for key, value in os.environ.items() if key not in ("LEOS_AGENT_ROOT", "PLUGIN_ROOT")}
            env.update({
                "HOME": str(base / "home"), "LEOS_AGENT_LOCAL_PATH": str(base / "data"),
                "CLAUDE_CONFIG_DIR": str(base / "claude"), "CODEX_HOME": str(base / "codex"),
                "HERMES_HOME": str(base / "hermes"), "PI_CODING_AGENT_DIR": str(base / "pi"),
                "OPENCODE_CONFIG_DIR": str(base / "opencode"),
                "OPENCODE_CONFIG": str(base / "opencode" / "opencode.jsonc"),
                "XDG_CONFIG_HOME": str(base / "xdg"), "LEOS_AGENT_PRICE_REFRESH": "off",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            (base / "home").mkdir()
            touched = set()
            for number, (harness, script, args, event) in enumerate(self.CALLS):
                log = base / f"touched-{number}.json"
                result = subprocess.run(
                    [sys.executable, "-c", AUDIT, str(ROOT), str(log), str(ROOT / "scripts" / script), *args],
                    input=json.dumps({**event, "cwd": tmp}), capture_output=True, text=True, timeout=30,
                    cwd=tmp, env={**env, "LEOS_AGENT_HARNESS": harness},
                )
                with self.subTest(harness=harness, script=script):
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue(result.stdout.strip(), result.stderr)
                touched.update(json.loads(log.read_text(encoding="utf-8")))
        # The hook really saw the run, not an empty set that would pass anything.
        self.assertLessEqual({"scripts/emit_payload.py", "scripts/observe_agent.py", "scripts/outcome.py",
                              "scripts/dispatch_guard.py", "rules/preferences.md"}, touched)
        self.assertEqual(sorted(touched - publish_npm.required_files()), [])


if __name__ == "__main__":
    unittest.main()
