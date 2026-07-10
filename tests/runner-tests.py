#!/usr/bin/env python3
"""Regression tests for the explicit council CLI runner.

The test uses local fake CLIs rather than real provider accounts. It verifies that structured
success is collected, "nothing returned" is a failure, sensitive prompts are not dispatched, and
a seat cannot recursively start Leo's council.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile


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


def run(repo, prompt, env, tier="elevated", cwd=None, checkpoint="impl"):
    return subprocess.run([sys.executable, RUNNER, "run", "--host", "codex", "--checkpoint", checkpoint,
                           "--tier", tier, "--prompt", prompt, "--cwd", cwd or repo],
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

    # JSONL bookkeeping without an agent message is not a completed review.
    _, repo, local, bindir, prompt, env = fresh()
    event_only = os.path.join(bindir, "codex")
    executable(event_only, "cat >/dev/null\nprintf '{\"type\":\"turn.completed\"}\\n'")
    write_seats(local, [event_only, "exec", "-"])
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("event-only Codex output lacks reviewer content", result.returncode == 1 and
          data["results"][0]["status"] == "missing-review-content")

    # OpenCode --format json emits JSONL text parts, not one JSON document.
    _, repo, local, bindir, prompt, env = fresh()
    codex = os.path.join(bindir, "codex")
    opencode = os.path.join(bindir, "opencode")
    executable(codex, "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    executable(opencode, "printf '{\"type\":\"step_start\"}\\n{\"type\":\"text\",\"part\":{\"type\":\"text\",\"text\":\"[]\"}}\\n'")
    write_opencode_external(local, [codex, "exec", "-"], [opencode, "run", "--agent", "plan"])
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
