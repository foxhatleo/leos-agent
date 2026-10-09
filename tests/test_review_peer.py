"""Behavioral tests for the opt-in cross-model review lens.

The peer CLIs are stand-in executables that reproduce what the real ones do
at this boundary (checked against their sources and docs): `codex login
status` exits 0 when logged in; `codex exec` reads the prompt from stdin for
`-`, prints a config summary with `model:` and `provider:` lines on stderr,
and writes its last message to the `-o` file; `claude auth status --json`
prints loggedIn/authMethod and exits 0 when logged in; `claude -p
--output-format json` prints one result object with `result`, `is_error`,
`total_cost_usd` and `modelUsage` keyed by the serving model.
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
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location("review_peer_test", ROOT / "scripts" / "review_peer.py")
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)

from test_ghreview import FakeGitHub, text_file  # noqa: E402

FAKE_CLI = r'''#!%(python)s
import json, os, sys, time
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
prompt = sys.stdin.read() if (args[:1] == ["exec"] or "-p" in args) else ""
with open(os.environ["FAKE_PEER_LOG"], "a") as fh:
    fh.write(json.dumps({"cli": name, "args": args, "stdin": prompt, "cwd": os.getcwd(),
                         "cwd_entries": os.listdir(".")}) + "\n")
ok = os.environ.get("FAKE_PEER_AUTH", "1") == "1"
reply = os.environ.get("FAKE_PEER_REPLY", '{"status": "done", "findings": [], "gaps": []}')
model = os.environ.get("FAKE_PEER_MODEL", "gpt-x" if name == "codex" else "claude-x")
if name == "codex" and args[:2] == ["login", "status"]:
    sys.stderr.write("Logged in using ChatGPT\n" if ok else "Not logged in\n")
    sys.exit(0 if ok else 1)
if name == "codex" and args[:1] == ["exec"]:
    time.sleep(float(os.environ.get("FAKE_PEER_SLEEP", "0")))
    sys.stderr.write("OpenAI Codex v0.0.0\n--------\nworkdir: %%s\nmodel: %%s\nprovider: openai\n"
                     "approval: never\nsandbox: read-only\n--------\nuser\n%%s\n" %% (os.getcwd(), model, prompt[:40]))
    with open(args[args.index("-o") + 1], "w") as fh:
        fh.write(reply)
    print(reply)
    sys.exit(0)
if name == "claude" and args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": ok, "authMethod": "claude.ai" if ok else "none", "apiProvider": "firstParty"}))
    sys.exit(0 if ok else 1)
if name == "claude" and "-p" in args:
    time.sleep(float(os.environ.get("FAKE_PEER_SLEEP", "0")))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply,
                      "total_cost_usd": 0.12, "modelUsage": {model: {"inputTokens": 10, "outputTokens": 99}}}))
    sys.exit(0)
sys.exit(64)
'''

SHA = "1" * 40
FILES = [text_file("src/a.py", "@@ -1,1 +1,2 @@\n ctx\n+risky()"),
         text_file("package-lock.json", "@@ -1 +1 @@\n-x\n+y")]


class PeerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.jsonl"
        sandbox = {key: str(self.tmp / key.lower()) for key in (
            "HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "PI_CODING_AGENT_DIR",
            "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME", "LEOS_AGENT_LOCAL_PATH")}
        sandbox["OPENCODE_CONFIG"] = str(self.tmp / "opencode.json")
        patcher = mock.patch.dict(os.environ, sandbox)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.github = FakeGitHub(SHA, FILES)
        gh = mock.patch.object(subprocess, "run", self.github.run)
        gh.start()
        self.addCleanup(gh.stop)

    def install(self, *names):
        for name in names:
            path = self.bin / name
            path.write_text(FAKE_CLI % {"python": sys.executable})
            path.chmod(0o755)

    def env(self, host, **extra):
        env = {"PATH": str(self.bin), "HOME": os.environ["HOME"], "FAKE_PEER_LOG": str(self.log)}
        if host == "claude":
            env["CLAUDECODE"] = "1"
        elif host == "codex":
            env["CODEX_THREAD_ID"] = "thread"
        env.update(extra)
        return env

    def which(self, name):
        return shutil.which(name, path=str(self.bin))

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def run_peer(self, host, target=None, user_enabled=True, context=None, **extra):
        out = self.tmp / "peer.json"
        args = types.SimpleNamespace(repo="owner/repo", pr=1, commit=SHA, out=str(out), target=target,
                                     context=context, user_enabled=user_enabled)
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            peer.cmd_run(args, env=self.env(host, **extra), which=self.which)
        return json.loads(out.read_text()), stderr.getvalue()


class TestOptIn(PeerCase):
    def test_nothing_is_sent_or_even_probed_unless_the_user_enabled_it(self):
        self.install("codex", "claude")
        artifact, _ = self.run_peer("claude", user_enabled=False)
        self.assertEqual(artifact["status"], "skipped")
        self.assertIn("disabled", artifact["reason"])
        self.assertEqual(self.calls(), [])

    def test_the_machine_setting_enables_it_and_says_what_leaves(self):
        self.install("codex")
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            peer.main(["config", "--enable"])
        self.assertIn("sends the pinned PR diff", stderr.getvalue())
        artifact, _ = self.run_peer("claude", user_enabled=False)
        self.assertEqual((artifact["status"], artifact["enabled_by"]), ("done", "machine setting"))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            peer.main(["config", "--disable"])
        self.assertEqual(self.run_peer("claude", user_enabled=False)[0]["status"], "skipped")


class TestRouteSelection(PeerCase):
    def test_a_missing_cli_or_a_missing_login_is_a_skip(self):
        artifact, _ = self.run_peer("claude")
        self.assertIn("not installed", artifact["reason"])
        self.install("codex")
        artifact, _ = self.run_peer("claude", FAKE_PEER_AUTH="0")
        self.assertIn("no login", artifact["reason"])
        self.assertFalse(any(c["args"][:1] == ["exec"] for c in self.calls()))

    def test_the_hosts_own_family_is_never_the_peer(self):
        self.install("codex", "claude")
        artifact, _ = self.run_peer("claude", target="claude")
        self.assertEqual(artifact["status"], "skipped")
        self.assertIn("own model family", artifact["reason"])

    def test_an_unattested_host_needs_a_named_target_and_never_verifies_independence(self):
        self.install("codex")
        self.assertIn("unattested", self.run_peer(None)[0]["reason"])
        artifact, _ = self.run_peer(None, target="codex")
        self.assertEqual(artifact["status"], "done")
        self.assertFalse(artifact["independence_verified"])

    def test_a_peer_whose_cli_reports_the_hosts_family_is_not_independent(self):
        self.install("codex")
        artifact, _ = self.run_peer("claude", FAKE_PEER_MODEL="claude-x-via-proxy")
        self.assertEqual(artifact["status"], "done")
        self.assertFalse(artifact["independence_verified"])


class TestCodexRoute(PeerCase):
    REPLY = json.dumps({"status": "done", "gaps": ["runtime config unknown"], "findings": [
        {"path": "src/a.py", "line": 2, "side": "RIGHT", "severity": "major", "confidence": 85,
         "note": "risky() raises on empty input"},
        {"path": "elsewhere.py", "line": 1, "note": "a path that was never sent"},
        {"path": "src/a.py", "line": "2", "note": "line is not a number"}]})

    def test_a_claude_host_sends_the_pinned_patches_to_a_read_only_codex(self):
        self.install("codex")
        artifact, stderr = self.run_peer("claude", FAKE_PEER_REPLY=self.REPLY)
        call = next(c for c in self.calls() if c["args"][:1] == ["exec"])
        args = call["args"]
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertEqual(args[-1], "-")
        self.assertEqual(call["cwd_entries"], [])
        self.assertFalse(os.path.exists(call["cwd"]))  # removed after the run
        self.assertIn("+risky()", call["stdin"])
        self.assertNotIn("package-lock.json", call["stdin"])
        self.assertEqual((artifact["status"], artifact["model_actual"], artifact["provider"]),
                         ("done", "gpt-x", "openai"))
        self.assertTrue(artifact["independence_verified"])
        self.assertFalse(artifact["counts_as_coverage"])
        self.assertEqual([f["note"] for f in artifact["findings"]], ["risky() raises on empty input"])
        self.assertEqual(artifact["dropped"], 2)
        self.assertEqual(artifact["sent_paths"], ["src/a.py"])
        self.assertIn("sending %d bytes of Owner/Repo#1" % artifact["bytes_sent"], stderr)

    def test_the_untrusted_block_is_fenced_with_a_fresh_marker(self):
        self.install("codex")
        self.run_peer("claude")
        self.run_peer("claude")
        fences = []
        for call in (c for c in self.calls() if c["args"][:1] == ["exec"]):
            marker = next(line for line in call["stdin"].splitlines() if line.startswith("<<UNTRUSTED-"))
            self.assertEqual(call["stdin"].count(marker), 3)  # named once in the rules, then open and close
            fences.append(marker)
        self.assertNotEqual(fences[0], fences[1])

    def test_patches_over_the_input_cap_are_listed_as_not_sent(self):
        self.install("codex")
        self.github.files = [text_file("big.py", "@@ -1 +1,2 @@\n x\n+" + "y" * 500),
                             text_file("small.py", "@@ -1 +1 @@\n-a\n+b")]
        from state import atomic_write, state_file
        atomic_write(state_file("review-peer"), {"max_input_bytes": 200})
        artifact, _ = self.run_peer("claude")
        self.assertEqual(artifact["sent_paths"], ["small.py"])
        self.assertEqual(artifact["not_sent"], [{"path": "big.py", "reason": "over the input cap"}])

    def test_a_reply_that_is_not_the_requested_json_is_a_failure(self):
        self.install("codex")
        artifact, _ = self.run_peer("claude", FAKE_PEER_REPLY="I looked and it seems fine.")
        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["findings"], [])

    def test_a_peer_that_overruns_its_time_limit_is_stopped(self):
        self.install("codex")
        from state import atomic_write, state_file
        atomic_write(state_file("review-peer"), {"timeout": 1})
        started = time.time()
        artifact, _ = self.run_peer("claude", FAKE_PEER_SLEEP="30")
        self.assertLess(time.time() - started, 15)
        self.assertEqual(artifact["status"], "failed")
        self.assertIn("timed out", artifact["reason"])


class TestClaudeRoute(PeerCase):
    def test_a_codex_host_uses_claude_without_tools_and_with_a_spending_cap(self):
        self.install("claude")
        reply = json.dumps({"status": "done", "findings": [
            {"path": "src/a.py", "line": 2, "severity": "blocking", "confidence": 95, "note": "crash"}]})
        artifact, _ = self.run_peer("codex", FAKE_PEER_REPLY=reply)
        args = next(c for c in self.calls() if "-p" in c["args"])["args"]
        self.assertEqual(args[args.index("--tools") + 1], "")
        for flag in ("--safe-mode", "--no-session-persistence"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--output-format") + 1], "json")
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "2.00")
        self.assertEqual((artifact["model_actual"], artifact["cost_usd"]), ("claude-x", 0.12))
        self.assertTrue(artifact["independence_verified"])
        self.assertEqual(artifact["findings"][0]["severity"], "blocking")


class TestDetect(PeerCase):
    def test_detect_reports_the_route_without_sending_anything(self):
        self.install("codex")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            peer.cmd_detect(types.SimpleNamespace(target=None, user_enabled=True),
                            env=self.env("claude", CODEX_SANDBOX_NETWORK_DISABLED="1"), which=self.which)
        decision = json.loads(out.getvalue())
        self.assertEqual((decision["status"], decision["target"], decision["host_family"]),
                         ("ready", "codex", "claude"))
        self.assertIn("escalation", decision["note"])
        self.assertEqual([c["args"] for c in self.calls()], [["login", "status"]])


if __name__ == "__main__":
    unittest.main()
