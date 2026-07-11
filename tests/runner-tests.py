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
    # bin/leos-python honors LEOS_LOCAL; the runner's council begin/end subprocesses go through
    # it, so the isolated local needs the real private venv reachable.
    real_venv = os.path.join(ROOT, "local", ".venv")
    if os.path.isdir(real_venv):
        os.symlink(real_venv, os.path.join(local, ".venv"))
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


def write_seats(local, native_argv, external_argv=None, native_extra=None, external_extra=None):
    seats = {
        "host": "codex",
        "native": dict({"mode": "exec", "transport": "stdin", "argv": native_argv,
                        "efforts": {"default": "high", "max": "xhigh"}}, **(native_extra or {})),
        "seats": [],
    }
    if external_argv:
        seats["seats"].append(dict({"name": "opus", "transport": "stdin", "argv": external_argv,
                                    "efforts": {"default": "high", "max": "xhigh"},
                                    "timeoutSeconds": 10}, **(external_extra or {})))
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
        approve_external=True, redact_sensitive=False, run_id=None, follow_up=False, seat=None):
    argv = [sys.executable, RUNNER, "run", "--host", "codex", "--checkpoint", checkpoint,
            "--tier", tier, "--prompt", prompt, "--cwd", cwd or repo]
    if external_only:
        argv.append("--external-only")
    if approve_external:
        argv.append("--approve-external")
    if redact_sensitive:
        argv.append("--redact-sensitive")
    if run_id:
        argv.extend(["--run-id", run_id])
    if follow_up:
        argv.append("--follow-up")
    if seat:
        argv.extend(["--seat", seat])
    try:
        return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
    except subprocess.TimeoutExpired as e:
        # One hung runner is a failing check with diagnostics, never a battery abort.
        return subprocess.CompletedProcess(argv, -999, stdout=(e.stdout or b"").decode() if
                                           isinstance(e.stdout, bytes) else (e.stdout or "{}"),
                                           stderr="runner timed out after 120s")


def wait_for_event(local, name, deadline=15):
    """Poll every isolated work dir's events.jsonl for a named lifecycle event; the runner
    flushes per event, so this observes progress live."""
    import glob
    end = time.time() + deadline
    pattern = os.path.join(local, "council", "work", "*", "*", "events.jsonl")
    while time.time() < end:
        for path in glob.glob(pattern):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if json.loads(line).get("event") == name:
                            return path
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(0.05)
    return None


def communicate_checked(proc, name, timeout):
    """communicate() that converts a hang into a failing check plus diagnostics instead of an
    unhandled TimeoutExpired that would abort the whole battery."""
    try:
        stdout, _stderr = proc.communicate(timeout=timeout)
        return stdout
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        check(name, False)
        print("  diagnostics — stdout tail:", (stdout or "")[-400:])
        print("  diagnostics — stderr tail:", (stderr or "")[-400:])
        return None


def pid_dead(pid, deadline=5):
    end = time.time() + deadline
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


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

    # Runner cancellation after seat launch is typed, bounded, and kills the seat's own session:
    # seat-started is emitted only once the child is registered (killable), so waiting on it is
    # deterministic rather than a fixed sleep.
    _, repo, local, bindir, prompt, env = fresh()
    sleeper = os.path.join(bindir, "codex")
    pid_receipt = os.path.join(local, "seat-pid.txt")
    executable(sleeper, f"echo $$ >'{pid_receipt}'\ncat >/dev/null\nsleep 30")
    write_seats(local, [sleeper, "exec", "-"])
    proc = subprocess.Popen([sys.executable, RUNNER, "run", "--host", "codex", "--checkpoint", "impl",
                             "--tier", "low", "--prompt", prompt, "--cwd", repo, "--approve-external"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    check("cancellation test observes a registered seat", wait_for_event(local, "seat-started") is not None)
    # Wait until the shim has actually written its pid (it may be killed pre-exec otherwise —
    # which is also a correct kill, but then there is nothing to assert against).
    end = time.time() + 10
    while time.time() < end and not os.path.exists(pid_receipt):
        time.sleep(0.02)
    check("cancellation test observes a running seat", os.path.exists(pid_receipt))
    proc.send_signal(signal.SIGTERM)
    stdout = communicate_checked(proc, "runner cancellation is typed and bounded", timeout=15)
    if stdout is not None:
        cancelled = json.loads(stdout)
        check("runner cancellation is typed and bounded", proc.returncode == 1 and
              cancelled["results"][0]["status"] == "cancelled")
        try:
            seat_pid = int(open(pid_receipt).read().strip())
        except (OSError, ValueError):
            seat_pid = None
        check("cancellation kills the seat process group", seat_pid is not None and pid_dead(seat_pid))

    # Cancellation BEFORE any seat launches never leaks an unkillable child and still writes a
    # typed result: the launch+registration critical section refuses under CANCELLED.
    _, repo, local, bindir, prompt, env = fresh()
    sleeper = os.path.join(bindir, "codex")
    pid_receipt = os.path.join(local, "seat-pid.txt")
    executable(sleeper, f"echo $$ >'{pid_receipt}'\ncat >/dev/null\nsleep 30")
    write_seats(local, [sleeper, "exec", "-"])
    proc = subprocess.Popen([sys.executable, RUNNER, "run", "--host", "codex", "--checkpoint", "impl",
                             "--tier", "low", "--prompt", prompt, "--cwd", repo, "--approve-external"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    check("early-cancel test observes runner start", wait_for_event(local, "runner-started") is not None)
    proc.send_signal(signal.SIGTERM)
    stdout = communicate_checked(proc, "pre-launch cancellation is bounded", timeout=15)
    if stdout is not None:
        early = json.loads(stdout)
        check("pre-launch cancellation is bounded and typed", proc.returncode == 1 and
              all(r["status"] == "cancelled" for r in early.get("results", [])) and
              os.path.isfile(early.get("resultPath", "")))
        if os.path.isfile(pid_receipt):
            seat_pid = int(open(pid_receipt).read().strip())
            check("no seat child survives an early cancel", pid_dead(seat_pid))
        else:
            check("no seat child survives an early cancel", True)   # never launched

    # A seat that finished its review before the run-wide signal stays completed; only the
    # still-running seat is cancelled.
    _, repo, local, bindir, prompt, env = fresh()
    fast = os.path.join(bindir, "codex")
    slow = os.path.join(bindir, "claude")
    executable(fast, "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    executable(slow, "cat >/dev/null\nsleep 30")
    write_seats(local, [fast, "exec", "-"], [slow, "--print"])
    proc = subprocess.Popen([sys.executable, RUNNER, "run", "--host", "codex", "--checkpoint", "impl",
                             "--tier", "elevated", "--prompt", prompt, "--cwd", repo, "--approve-external"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    check("mixed-cancel test sees the fast seat finish", wait_for_event(local, "seat-finished") is not None)
    proc.send_signal(signal.SIGTERM)
    stdout = communicate_checked(proc, "completed seat survives run-wide cancel", timeout=15)
    if stdout is not None:
        mixed = json.loads(stdout)
        statuses = {r["seat"]: r["status"] for r in mixed.get("results", [])}
        check("completed seat survives run-wide cancel",
              statuses.get("native") == "completed" and statuses.get("opus") == "cancelled")

    # An externally SIGKILLed seat with no cancellation is signal-exit, not cancelled.
    _, repo, local, bindir, prompt, env = fresh()
    suicidal = os.path.join(bindir, "codex")
    executable(suicidal, "cat >/dev/null\nkill -KILL $$")
    write_seats(local, [suicidal, "exec", "-"])
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("external SIGKILL is signal-exit", result.returncode == 1 and
          data["results"][0]["status"] == "signal-exit")

    # A dead orchestrator stderr pipe must not kill the run before result.json is written.
    _, repo, local, bindir, prompt, env = fresh()
    fine = os.path.join(bindir, "codex")
    executable(fine, "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    write_seats(local, [fine, "exec", "-"])
    proc = subprocess.Popen([sys.executable, RUNNER, "run", "--host", "codex", "--checkpoint", "impl",
                             "--tier", "low", "--prompt", prompt, "--cwd", repo, "--approve-external"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    proc.stderr.close()
    dead_out = proc.stdout.read()
    rc = proc.wait(timeout=30)
    dead_data = json.loads(dead_out)
    check("dead orchestrator stderr does not kill the run", rc == 0 and
          os.path.isfile(dead_data.get("resultPath", "")) and
          dead_data["results"][0]["status"] == "completed")

    # Seats get the recursion sentinel but never the run-ownership token.
    _, repo, local, bindir, prompt, env = fresh()
    env_receipt = os.path.join(local, "seat-env.txt")
    envdump = os.path.join(bindir, "codex")
    executable(envdump, f"cat >/dev/null\nenv >'{env_receipt}'\nprintf '{{\"type\":\"item.completed\",\"item\":{{\"type\":\"agent_message\",\"text\":\"[]\"}}}}\\n'")
    write_seats(local, [envdump, "exec", "-"])
    result = run(repo, prompt, env, tier="low")
    seat_env = open(env_receipt).read()
    check("seat env keeps LEOS_COUNCIL_SEAT and omits the run token",
          result.returncode == 0 and "LEOS_COUNCIL_SEAT=1" in seat_env and
          "LEOS_COUNCIL_ACTIVE_RUN" not in seat_env)

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

    # An unknown binary without an explicit adapter is an invalid seat, never inferred raw.
    _, repo, local, bindir, prompt, env = fresh()
    mystery = os.path.join(bindir, "mystery-cli")
    executable(mystery, "cat >/dev/null\nprintf '{\"anything\": true}\\n'")
    write_seats(local, [mystery, "exec", "-"])
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("unknown binary is invalid-seat-config", result.returncode == 1 and
          data["results"][0]["status"] == "invalid-seat-config" and
          "adapter" in data["results"][0].get("reason", ""))
    executable(mystery, "cat >/dev/null\nprintf 'not json'")
    write_seats(local, [mystery, "exec", "-"], native_extra={"adapter": "weird"})
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("bogus adapter string is refused", result.returncode == 1 and
          data["results"][0]["status"] == "invalid-seat-config")

    # Explicit raw stays available and still enforces the findings contract. (Each completing
    # run holds its checkpoint marker, so scenarios that follow a success need a fresh repo.)
    _, repo, local, bindir, prompt, env = fresh()
    rawcli = os.path.join(bindir, "custom-reviewer")
    executable(rawcli, "cat >/dev/null\nprintf '[]'")
    write_seats(local, [rawcli, "exec", "-"], native_extra={"adapter": "raw"})
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("explicit raw adapter completes", result.returncode == 0 and
          data["results"][0]["status"] == "completed")
    _, repo, local, bindir, prompt, env = fresh()
    rawcli = os.path.join(bindir, "custom-reviewer")
    executable(rawcli, "cat >/dev/null\nprintf 'looks good to me'")
    write_seats(local, [rawcli, "exec", "-"], native_extra={"adapter": "raw"})
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("explicit raw adapter still enforces the findings contract", result.returncode == 1 and
          data["results"][0]["status"] == "invalid-review-findings")

    # A plan review whose externals all fail and whose native block is missing yields a typed
    # fallback error and releases the marker — never an uncaught crash + leaked marker.
    _, repo, local, bindir, prompt, env = fresh()
    failing = os.path.join(bindir, "claude")
    executable(failing, "cat >/dev/null\nexit 7")
    with open(os.path.join(local, "seats.codex.json"), "w") as f:
        json.dump({"host": "codex", "seats": [{"name": "opus", "transport": "stdin",
                   "argv": [failing, "--print"], "efforts": {"default": "high", "max": "xhigh"},
                   "timeoutSeconds": 10}]}, f)
    result = run(repo, prompt, env, tier="low", checkpoint="plan")
    data = json.loads(result.stdout)
    fallback_rows = [r for r in data.get("results", []) if r.get("fallback")]
    check("plan fallback without native is typed", result.returncode == 1 and
          fallback_rows and fallback_rows[0]["status"] == "invalid-seat-config" and
          os.path.isfile(data.get("resultPath", "")))
    retry = run(repo, prompt, env, tier="low", checkpoint="plan")
    check("typed fallback failure releases the marker",
          "nested-leos-council-refused" not in retry.stdout)

    # Seats run in a per-seat scratch project root under the work dir, removed afterwards; the
    # prompt header names the reviewed repo; "cwd": "repo" opts back into the repo cwd.
    _, repo, local, bindir, prompt, env = fresh()
    pwd_receipt = os.path.join(local, "seat-pwd.txt")
    prompt_receipt = os.path.join(local, "seat-prompt.txt")
    pwdcli = os.path.join(bindir, "codex")
    executable(pwdcli, f"pwd >'{pwd_receipt}'\ncat >'{prompt_receipt}'\nprintf '{{\"type\":\"item.completed\",\"item\":{{\"type\":\"agent_message\",\"text\":\"[]\"}}}}\\n'")
    write_seats(local, [pwdcli, "exec", "-"])
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    seat_pwd = open(pwd_receipt).read().strip()
    work_dir = os.path.dirname(data["resultPath"])
    check("seat runs in a scratch cwd under the work dir", result.returncode == 0 and
          seat_pwd.endswith("cwd-native") and
          os.path.realpath(os.path.dirname(seat_pwd)) == os.path.realpath(work_dir) and
          seat_pwd != os.path.realpath(repo))
    check("scratch cwd is removed after the seat", not os.path.exists(os.path.join(work_dir, "cwd-native")))
    check("result rows carry cwdMode", data["results"][0].get("cwdMode") == "scratch")
    header = open(prompt_receipt).read()
    check("prompt header names the reviewed repo",
          header.startswith(f"Repository under review (absolute path): {os.path.realpath(repo)}"))
    _, repo, local, bindir, prompt, env = fresh()
    pwd_receipt = os.path.join(local, "seat-pwd.txt")
    pwdcli = os.path.join(bindir, "codex")
    executable(pwdcli, f"pwd >'{pwd_receipt}'\ncat >/dev/null\nprintf '{{\"type\":\"item.completed\",\"item\":{{\"type\":\"agent_message\",\"text\":\"[]\"}}}}\\n'")
    write_seats(local, [pwdcli, "exec", "-"], native_extra={"cwd": "repo"})
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("cwd:repo opts back into the reviewed repo", result.returncode == 0 and
          open(pwd_receipt).read().strip() == os.path.realpath(repo) and
          data["results"][0].get("cwdMode") == "repo")
    _, repo, local, bindir, prompt, env = fresh()
    pwdcli = os.path.join(bindir, "codex")
    executable(pwdcli, "cat >/dev/null\nprintf 'x'")
    write_seats(local, [pwdcli, "exec", "-"], native_extra={"cwd": "home"})
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    check("invalid cwd value is invalid-seat-config", result.returncode == 1 and
          data["results"][0]["status"] == "invalid-seat-config")

    # Self-review regression: when LEOS_LOCAL (and therefore the scratch work dir) is physically
    # inside the reviewed repository, the seat still sees the scratch as its own Git root. Parent
    # AGENTS.md/project config must not regain authority through repository discovery.
    _, repo, _unused_local, bindir, prompt, env = fresh()
    self_local = os.path.join(repo, "local")
    os.makedirs(self_local)
    real_venv = os.path.join(ROOT, "local", ".venv")
    if os.path.isdir(real_venv):
        os.symlink(real_venv, os.path.join(self_local, ".venv"))
    env = dict(env, LEOS_LOCAL=self_local,
               LEOS_COUNCIL_STATE=os.path.join(self_local, "council", "state"))
    root_receipt = os.path.join(os.path.dirname(repo), "seat-git-root.txt")
    self_cli = os.path.join(bindir, "codex")
    executable(self_cli, f"git rev-parse --show-toplevel >'{root_receipt}'\ncat >/dev/null\n"
               "printf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    write_seats(self_local, [self_cli, "exec", "-"])
    result = run(repo, prompt, env, tier="low")
    data = json.loads(result.stdout)
    discovered_root = open(root_receipt).read().strip()
    check("self-review scratch establishes a distinct project root", result.returncode == 0 and
          discovered_root != os.path.realpath(repo) and
          discovered_root.endswith("cwd-native") and
          discovered_root.startswith(os.path.realpath(os.path.join(self_local, "council", "work"))))

    # The fix->re-review pass is first-class: a finished --run-id cannot be reused (round-1
    # artifacts immutable), --follow-up reuses the active marker into <run>/pass-2/, a third
    # pass is refused, and --seat selects exactly the named re-review seat.
    _, repo, local, bindir, prompt, env = fresh()
    okc = os.path.join(bindir, "codex")
    okcl = os.path.join(bindir, "claude")
    executable(okc, "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    executable(okcl, "cat >/dev/null\nprintf '{\"result\":\"[]\"}\\n'")
    write_seats(local, [okc, "exec", "-"], [okcl, "--print"])
    first = run(repo, prompt, env, tier="elevated", run_id="r1pass")
    first_data = json.loads(first.stdout)
    check("follow-up fixture first pass completes", first.returncode == 0)
    pass1_result = first_data["resultPath"]
    pass1_bytes = open(pass1_result, "rb").read()
    reuse = run(repo, prompt, env, tier="elevated", run_id="r1pass")
    check("reusing a finished run-id without --follow-up is refused",
          reuse.returncode == 2 and "run-id-work-exists" in reuse.stdout and
          open(pass1_result, "rb").read() == pass1_bytes)
    bad_seat = run(repo, prompt, env, tier="elevated", follow_up=True, seat="nosuch")
    check("follow-up with an unconfigured seat is typed", bad_seat.returncode == 2 and
          "seat-not-configured" in bad_seat.stdout)
    seat_no_fu = run(repo, prompt, env, tier="elevated", seat="opus")
    check("--seat without --follow-up is refused", seat_no_fu.returncode == 2 and
          "seat-requires-follow-up" in seat_no_fu.stdout)
    second = run(repo, prompt, env, tier="elevated", follow_up=True, seat="opus")
    second_data = json.loads(second.stdout)
    check("follow-up dispatches exactly the named seat under pass-2", second.returncode == 0 and
          [r["seat"] for r in second_data["results"]] == ["opus"] and
          second_data["resultPath"] == os.path.join(os.path.dirname(pass1_result), "pass-2", "result.json") and
          second_data.get("pass") == 2)
    check("follow-up preserves round-1 artifacts", open(pass1_result, "rb").read() == pass1_bytes)
    third = run(repo, prompt, env, tier="elevated", follow_up=True, seat="opus")
    check("a third pass is refused", third.returncode == 2 and
          "follow-up-passes-exhausted" in third.stdout)

    # Follow-up without any active marker is a typed refusal.
    _, repo, local, bindir, prompt, env = fresh()
    executable(os.path.join(bindir, "codex"), "cat >/dev/null\nprintf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"[]\"}}\\n'")
    write_seats(local, [os.path.join(bindir, "codex"), "exec", "-"])
    fu = run(repo, prompt, env, tier="low", follow_up=True)
    check("follow-up without an active run is typed", fu.returncode == 2 and
          "no-active-run-for-follow-up" in fu.stdout)

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
