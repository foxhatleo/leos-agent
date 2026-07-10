#!/usr/bin/env python3
"""Regression tests for the explicit council CLI runner.

The test uses local fake CLIs rather than real provider accounts. It verifies that structured
success is collected, "nothing returned" is a failure, sensitive prompts are not dispatched, and
a seat cannot recursively start Leo's council.
"""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TEST_TMP = os.path.join(ROOT, "local", "test-work")
os.makedirs(TEST_TMP, exist_ok=True)
tempfile.tempdir = TEST_TMP
RUNNER = os.path.join(ROOT, "core", "council", "bin", "runner.py")

passed = failed = 0
cleanup = []


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


def executable(path, body):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def fresh():
    root = tempfile.mkdtemp(prefix="runner.")
    cleanup.append(root)
    repo, local, bindir = (os.path.join(root, n) for n in ("repo", "local", "bin"))
    os.makedirs(repo); os.makedirs(local); os.makedirs(bindir)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("# test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "base"], cwd=repo, check=True)
    prompt = os.path.join(root, "prompt.md")
    with open(prompt, "w") as f:
        f.write("Review this safe test change.\n")
    env = dict(os.environ, LEOS_LOCAL=local, LEOS_COUNCIL_STATE=os.path.join(local, "council", "state"))
    return root, repo, local, bindir, prompt, env


def write_seats(local, native_argv, external_argv=None):
    seats = {
        "host": "codex",
        "native": {"mode": "exec", "transport": "stdin", "argv": native_argv,
                   "efforts": {"default": "high", "max": "xhigh"}},
        "seats": [],
    }
    if external_argv:
        seats["seats"].append({"name": "opus", "transport": "stdin", "argv": external_argv,
                               "efforts": {"default": "high", "max": "xhigh"}, "timeoutSeconds": 10})
    with open(os.path.join(local, "seats.codex.json"), "w") as f:
        json.dump(seats, f)


def write_subagent_native(local):
    with open(os.path.join(local, "seats.codex.json"), "w") as f:
        json.dump({"host": "codex", "native": {"mode": "subagent", "model": "opus"}, "seats": []}, f)


def write_opencode_external(local, native_argv, opencode_argv):
    with open(os.path.join(local, "seats.codex.json"), "w") as f:
        json.dump({
            "host": "codex",
            "native": {"mode": "exec", "transport": "stdin", "argv": native_argv,
                       "efforts": {"default": "high", "max": "xhigh"}},
            "seats": [{"name": "glm", "transport": "arg",
                       "argv": opencode_argv + ["{PROMPT_TEXT}"],
                       "efforts": {"default": "high", "max": "max"}, "timeoutSeconds": 10}],
        }, f)


def write_cursor_external(local, native_argv, cursor_argv, response_path="result"):
    with open(os.path.join(local, "seats.codex.json"), "w") as f:
        json.dump({
            "host": "codex",
            "native": {"mode": "exec", "transport": "stdin", "argv": native_argv,
                       "efforts": {"default": "high", "max": "xhigh"}},
            "seats": [{"name": "grok", "transport": "arg",
                       "argv": cursor_argv + ["{PROMPT_TEXT}"], "adapter": "cursor-json",
                       "responsePath": response_path,
                       "efforts": {"default": "high", "max": "xhigh"}, "timeoutSeconds": 10}],
        }, f)


def run(repo, prompt, env, tier="elevated", cwd=None, checkpoint="impl", external_only=False,
        approve_external=True, redact_sensitive=False):
    argv = [sys.executable, RUNNER, "run", "--host", "codex", "--checkpoint", checkpoint,
            "--tier", tier, "--prompt", prompt, "--cwd", cwd or repo]
    if external_only:
        argv.append("--external-only")
    if approve_external:
        argv.append("--approve-external")
    if redact_sensitive:
        argv.append("--redact-sensitive")
    return subprocess.run(argv,
                          capture_output=True, text=True, env=env)


def main():
    # Structured Claude/Codex adapters complete, preserve result files, and run in parallel.
    _, repo, local, bindir, prompt, env = fresh()
    codex = os.path.join(bindir, "codex")
    claude = os.path.join(bindir, "claude")
    executable(codex, "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    executable(claude, "cat >/dev/null\nprintf '{\"result\":\"[]\"}\\n'")
    write_seats(local, [codex, "exec", "-"], [claude, "--print"])
    result = run(repo, prompt, env)
    data = json.loads(result.stdout)
    statuses = {entry["seat"]: entry["status"] for entry in data.get("results", [])}
    check("structured seats complete", result.returncode == 0 and statuses == {"native": "completed", "opus": "completed"})
    check("runner writes private result under local", data.get("resultPath", "").startswith(local) and os.path.isfile(data["resultPath"]))
    events = open(os.path.join(os.path.dirname(data["resultPath"]), "events.jsonl"), encoding="utf-8").read()
    check("runner records progress lifecycle", "runner-started" in events and "seat-started" in events and "runner-finished" in events)

    # A clean exit with empty stdout is the observed "nothing returned" failure, not a pass.
    _, repo, local, bindir, prompt, env = fresh()
    empty = os.path.join(bindir, "codex")
    executable(empty, "cat >/dev/null\nexit 0")
    write_seats(local, [empty, "exec", "-"])
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("empty CLI output is classified", result.returncode == 1 and data["results"][0]["status"] == "empty-output")
    retry = run(repo, prompt, env, tier="low")
    check("failed dispatch releases active marker for retry", retry.returncode == 1 and
          "nested-leos-council-refused" not in retry.stdout)

    # JSONL bookkeeping without an agent message is not a completed review.
    _, repo, local, bindir, prompt, env = fresh()
    event_only = os.path.join(bindir, "codex")
    executable(event_only, "cat >/dev/null\nprintf '{\"type\":\"turn.completed\"}\\n'")
    write_seats(local, [event_only, "exec", "-"])
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("event-only Codex output lacks reviewer content", result.returncode == 1 and
          data["results"][0]["status"] == "missing-review-content")

    # Runner cancellation terminates the whole seat process group and records a typed failure.
    _, repo, local, bindir, prompt, env = fresh()
    sleeper = os.path.join(bindir, "codex")
    executable(sleeper, "cat >/dev/null\nsleep 30")
    write_seats(local, [sleeper, "exec", "-"])
    proc = subprocess.Popen([sys.executable, RUNNER, "run", "--host", "codex", "--checkpoint", "impl",
                             "--tier", "low", "--prompt", prompt, "--cwd", repo, "--approve-external"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=10)
    cancelled = json.loads(stdout)
    check("runner cancellation is typed and bounded", proc.returncode == 1 and
          cancelled["results"][0]["status"] == "cancelled")

    # Nonempty reviewer prose is not enough: the committed prompts require a JSON findings array.
    _, repo, local, bindir, prompt, env = fresh()
    invalid = os.path.join(bindir, "codex")
    executable(invalid, "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"Looks good\"}}\\n'")
    write_seats(local, [invalid, "exec", "-"])
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("non-schema reviewer prose is rejected", result.returncode == 1 and
          data["results"][0]["status"] == "invalid-review-findings")

    # OpenCode --format json emits JSONL text parts, not one JSON document.
    _, repo, local, bindir, prompt, env = fresh()
    codex = os.path.join(bindir, "codex")
    opencode = os.path.join(bindir, "opencode")
    executable(codex, "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    executable(opencode, "printf '{\"type\":\"step_start\"}\\n{\"type\":\"text\",\"part\":{\"type\":\"text\",\"text\":\"[]\"}}\\n'")
    write_opencode_external(local, [codex, "exec", "-"], [opencode, "run", "--agent", "plan"])
    with open(prompt, "w") as f:
        f.write('Review JSON-shaped code: const value = {"nested": {"ok": true}};\n')
    result = run(repo, prompt, env, tier="elevated")
    data = json.loads(result.stdout)
    check("OpenCode JSONL text output completes", result.returncode == 0 and
          {item["seat"]: item["status"] for item in data["results"]} == {"native": "completed", "glm": "completed"})

    # A Cursor JSON contract is accepted only with its verified reviewer-text field.
    _, repo, local, bindir, prompt, env = fresh()
    codex = os.path.join(bindir, "codex")
    cursor = os.path.join(bindir, "cursor-agent")
    executable(codex, "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    executable(cursor, "printf '{\"result\":\"[]\"}\\n'")
    write_cursor_external(local, [codex, "exec", "-"], [cursor, "-p", "--mode", "plan"])
    result = run(repo, prompt, env, tier="elevated")
    data = json.loads(result.stdout)
    check("Cursor responsePath extracts reviewer content", result.returncode == 0 and
          {item["seat"]: item["status"] for item in data["results"]} == {"native": "completed", "grok": "completed"})

    # Never dispatch a likely credential prompt by default.
    _, repo, local, bindir, prompt, env = fresh()
    with open(prompt, "w") as f:
        f.write("DATABASE_URL=postgres://review:password@db.example/internal\n")
    executable(os.path.join(bindir, "codex"), "exit 99")
    write_seats(local, [os.path.join(bindir, "codex"), "exec", "-"])
    result = run(repo, prompt, env, tier="low")
    check("sensitive prompt is refused before dispatch", result.returncode == 2 and "sensitive-prompt-refused" in result.stdout)

    # Sensitive fixtures can proceed only through deterministic redaction; raw material never reaches stdin.
    received = os.path.join(local, "received.txt")
    redactor = os.path.join(bindir, "codex")
    executable(redactor, f"cat >'{received}'\nprintf '{{\"type\":\"item.completed\",\"item\":{{\"type\":\"agent_message\",\"text\":\"[]\"}}}}\\n'")
    write_seats(local, [redactor, "exec", "-"])
    result = run(repo, prompt, env, tier="low", redact_sensitive=True)
    check("sensitive fixture is redacted before dispatch", result.returncode == 0 and
          "password" not in open(received).read() and "REDACTED" in open(received).read())

    # A seat inherits the sentinel and cannot start a nested Leo council.
    _, repo, local, bindir, prompt, env = fresh()
    executable(os.path.join(bindir, "codex"), "exit 99")
    write_seats(local, [os.path.join(bindir, "codex"), "exec", "-"])
    nested = dict(env, LEOS_COUNCIL_SEAT="1")
    result = run(repo, prompt, nested, tier="low")
    check("nested council is refused", result.returncode == 3 and "nested-leos-council-refused" in result.stdout)

    # Runner never pretends it can invoke a host-native subagent: that is orchestrator authority.
    _, repo, local, _, prompt, env = fresh()
    write_subagent_native(local)
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("native subagent remains orchestrator-owned", result.returncode == 0 and
          data.get("dispatchOk") is True and data.get("reviewComplete") is False and
          data.get("requiresOrchestratorNative") is True)
    native_review = os.path.join(os.path.dirname(data["resultPath"]), "native-review.json")
    with open(native_review, "w") as f:
        f.write("[]\n")
    collected = subprocess.run([sys.executable, RUNNER, "collect-native", "--result", data["resultPath"],
                                "--seat", "native", "--review-file", native_review],
                               capture_output=True, text=True, env=env)
    collected_data = json.loads(collected.stdout)
    check("native subagent result can be collected mechanically", collected.returncode == 0 and
          collected_data["reviewComplete"] is True and
          collected_data["results"][0]["transportResult"]["findings"] == [])

    # Native-only fallback preserves independent-pass depth at higher tiers.
    _, repo, local, bindir, prompt, env = fresh()
    executable(os.path.join(bindir, "codex"), "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    write_seats(local, [os.path.join(bindir, "codex"), "exec", "-"])
    result = run(repo, prompt, env, tier="high")
    data = json.loads(result.stdout)
    check("native-only high tier runs three independent passes", result.returncode == 0 and
          [item["seat"] for item in data["results"]] == ["native", "native-2", "native-3"])

    # Planning is external-first: it should not waste a native pass when an independent CLI seat
    # is configured, and normal plans use just the first strong external reviewer.
    _, repo, local, bindir, prompt, env = fresh()
    executable(os.path.join(bindir, "codex"), "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    executable(os.path.join(bindir, "claude"), "cat >/dev/null\nprintf '{\"result\":\"[]\"}\\n'")
    write_seats(local, [os.path.join(bindir, "codex"), "exec", "-"], [os.path.join(bindir, "claude"), "--print"])
    result = run(repo, prompt, env, tier="low", checkpoint="plan")
    data = json.loads(result.stdout)
    check("plan checkpoint uses configured external before native", result.returncode == 0 and
          [item["seat"] for item in data["results"]] == ["opus"])
    result = run(repo, prompt, env, tier="low", checkpoint="plan", approve_external=False)
    check("external dispatch requires explicit project approval", result.returncode == 2 and
          "external-send-approval-required" in result.stdout)

    # A failed plan external triggers a native fallback, but the failed council remains incomplete.
    _, repo, local, bindir, prompt, env = fresh()
    native = os.path.join(bindir, "codex")
    external = os.path.join(bindir, "claude")
    executable(native, "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    executable(external, "cat >/dev/null\nexit 7")
    write_seats(local, [native, "exec", "-"], [external, "--print"])
    result = run(repo, prompt, env, tier="low", checkpoint="plan")
    data = json.loads(result.stdout)
    check("failed plan external triggers native fallback", result.returncode == 1 and
          [item["seat"] for item in data["results"]] == ["opus", "native"] and
          data["results"][1]["status"] == "completed" and data["reviewComplete"] is False)

    # Vacuous selection is never a successful review.
    _, repo, local, _, prompt, env = fresh()
    write_subagent_native(local)
    result = run(repo, prompt, env, tier="low", external_only=True)
    data = json.loads(result.stdout)
    check("zero selected seats is incomplete", result.returncode == 1 and
          data["dispatchOk"] is False and data["reviewComplete"] is False)

    # A runner launched from a package directory normalizes to the git root, so the engine's
    # active marker blocks a second root-level council instead of permitting recursion by path.
    _, repo, local, bindir, prompt, env = fresh()
    subdir = os.path.join(repo, "packages", "one")
    os.makedirs(subdir)
    executable(os.path.join(bindir, "codex"), "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    write_seats(local, [os.path.join(bindir, "codex"), "exec", "-"])
    first = run(repo, prompt, env, tier="low", cwd=subdir)
    first_data = json.loads(first.stdout)
    second = run(repo, prompt, env, tier="low", cwd=repo)
    check("subdirectory runner normalizes repository root", first.returncode == 0 and first_data.get("cwd") == repo and
          second.returncode == 3 and "nested-leos-council-refused" in second.stdout)

    total = passed + failed
    print(f"runner-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    for path in cleanup:
        shutil.rmtree(path, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
