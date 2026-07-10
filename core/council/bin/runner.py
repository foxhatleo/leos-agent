#!/usr/bin/env python3
"""Deterministic external-seat runner for Leo's Agents councils.

This is deliberately an adapter, not an autonomous council trigger.  An orchestrator calls
``runner.py run`` only after it has decided to review and prepared a prompt.  The runner then
selects the configured CLI seats, marks the review active before dispatch, invokes direct argv
arrays (never a shell), and writes private structured results under ``local/council/work``.

Native host subagents remain orchestrator-owned: a Claude-native ``mode: subagent`` is reported as
``orchestrator-native-subagent-required`` with the private prompt path.  It is not approximated by
secretly launching another council or by granting the runner host-agent authority.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent.parent.parent
LOCAL = Path(os.environ.get("LEOS_LOCAL", ROOT / "local"))
COUNCIL = HERE.parent / "council.py"
PYTHON = ROOT / "bin" / "leos-python"
STATE_ROOT = Path(os.environ.get("LEOS_COUNCIL_STATE", LOCAL / "council" / "state"))
WORK_ROOT = LOCAL / "council" / "work"
IN_REVIEW_TTL = 1800
MAX_PROMPT_BYTES = 4 * 1024 * 1024
MAX_ARG_PROMPT_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
HOSTS = ("claude", "codex", "opencode", "cursor")
TIERS = ("low", "elevated", "high", "critical")

# Deliberately narrow: this catches values/blocks that are very likely credentials without
# treating ordinary source code that mentions "token" as secret material.
SENSITIVE_PROMPT_RE = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"
    r"|^\s*(?:[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)|"
    r"aws_secret_access_key|database_url|redis_url|mongodb_uri|connection_string)\s*[:=]\s*[^\s#]{8,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\b(?:ghp_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_-]{12,}\b"
    r"|(?i:authorization\s*:\s*bearer\s+)[A-Za-z0-9._-]{12,}"
    r"|(?i://[^\s/@:]+:[^\s/@]+@[^\s/]+)"
    r"|^diff --git a/(?:[^\s]*/)?(?:\.env[^\s]*|[^\s]+\.(?:pem|key))\s+b/"
    r"|^\+\+\+ b/(?:[^\s]*/)?(?:\.env[^\s]*|[^\s]+\.(?:pem|key))$"
    , re.IGNORECASE | re.MULTILINE
)


def secure_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def write_private(path, data, binary=False):
    secure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix="runner-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        if binary:
            f = os.fdopen(fd, "wb")
        else:
            f = os.fdopen(fd, "w", encoding="utf-8")
        with f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def write_json(path, data):
    write_private(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def project_root(cwd):
    """Resolve a repository root when possible; fall back for non-git plan review locations."""
    root = os.path.realpath(cwd)
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, text=True,
                                capture_output=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            root = os.path.realpath(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return root


def project_slug(cwd):
    """Use the same git-top-level identity as council.py, even when the orchestrator is in a
    package subdirectory.  Otherwise runner and engine would write different active markers."""
    root = project_root(cwd)
    base = re.sub(r"[^A-Za-z0-9]+", "-", os.path.basename(root)).strip("-") or "repo"
    digest = hashlib.sha256(root.encode("utf-8", "replace")).hexdigest()[:10]
    return f"{base}-{digest}"


def in_review_path(cwd):
    return STATE_ROOT / project_slug(cwd) / "in-review.json"


def fresh_active_run(cwd):
    data = read_json(in_review_path(cwd), {})
    if not isinstance(data, dict):
        return None
    if int(time.time()) - int(data.get("ts", 0)) >= IN_REVIEW_TTL:
        return None
    return data


class EventLog:
    """Private structured lifecycle events plus terse stderr progress for an orchestrator waiting
    on final JSON.  Events intentionally contain no prompt or model output."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def emit(self, event, **fields):
        record = {"ts": int(time.time()), "event": event, **fields}
        with self.lock:
            secure_dir(self.path.parent)
            with open(self.path, "a", encoding="utf-8") as f:
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                f.write(json.dumps(record, sort_keys=True) + "\n")
                f.flush()
            label = fields.get("seat")
            suffix = f" ({label})" if label else ""
            sys.stderr.write(f"[council-runner] {event}{suffix}\n")
            sys.stderr.flush()


def tier_external_count(tier, available):
    return {"low": 0, "elevated": 1, "high": 2, "critical": available}[tier]


def substitute(value, substitutions):
    if not isinstance(value, str):
        return value
    for needle, replacement in substitutions.items():
        value = value.replace(needle, replacement)
    return value


def adapter_for(argv):
    base = os.path.basename(argv[0]) if argv else ""
    if base == "claude":
        return "claude"
    if base == "codex":
        return "codex"
    if base == "opencode":
        return "opencode"
    if base == "cursor-agent":
        return "cursor-unverified"
    return "raw"


def insert_before_prompt(argv, flags):
    if argv and argv[-1] == "-":
        return argv[:-1] + flags + ["-"]
    return argv + flags


def prepare_command(seat, tier, prompt):
    """Resolve a machine-local seat into a direct CLI argv plus output contract."""
    efforts = seat.get("efforts") if isinstance(seat.get("efforts"), dict) else {}
    effort = efforts.get("max" if tier == "critical" else "default", "high")
    isolated = str(LOCAL / "isolated-codex-home")
    substitutions = {"{EFFORT}": str(effort), "{ISOLATED_CODEX_HOME}": isolated}
    argv = [substitute(v, substitutions) for v in seat.get("argv", [])]
    if not argv or any(not isinstance(v, str) or not v for v in argv):
        raise ValueError("seat argv must be a nonempty string array")
    transport = seat.get("transport")
    if transport not in ("stdin", "arg"):
        raise ValueError("seat transport must be stdin or arg")
    if transport == "arg":
        if "{PROMPT_TEXT}" not in argv:
            raise ValueError("arg seat must include the literal {PROMPT_TEXT} placeholder")
        if len(prompt.encode("utf-8")) > MAX_ARG_PROMPT_BYTES:
            raise ValueError("arg transport prompt exceeds 128 KiB; choose a stdin-capable transport")
        argv = [prompt if v == "{PROMPT_TEXT}" else v for v in argv]
    elif "{PROMPT_TEXT}" in argv:
        raise ValueError("stdin seat must not contain {PROMPT_TEXT}")
    if any("{" in value and "}" in value for value in argv):
        raise ValueError("unresolved placeholder in seat argv")

    adapter = seat.get("adapter") if isinstance(seat.get("adapter"), str) else adapter_for(argv)
    if adapter == "cursor-unverified":
        raise ValueError("Cursor seat needs an explicit adapter: cursor-json after setup validates its JSON output contract")
    if adapter == "cursor-json":
        response_path = seat.get("responsePath")
        if not isinstance(response_path, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", response_path):
            raise ValueError("cursor-json seat needs a simple responsePath to nonempty reviewer text")
        adapter = f"cursor-json:{response_path}"
    if adapter == "claude" and "--output-format" not in argv:
        argv = insert_before_prompt(argv, ["--output-format", "json"])
    elif adapter == "codex" and "--json" not in argv:
        argv = insert_before_prompt(argv, ["--json"])
    elif adapter == "opencode" and "--format" not in argv:
        # The argument transport's prompt is final; options must precede it.
        argv = argv[:-1] + ["--format", "json"] + argv[-1:] if transport == "arg" else argv + ["--format", "json"]

    env = seat.get("env") if isinstance(seat.get("env"), dict) else {}
    env = {str(k): substitute(str(v), substitutions) for k, v in env.items()}
    timeout = seat.get("timeoutSeconds", 300)
    if not isinstance(timeout, int) or not 1 <= timeout <= 900:
        raise ValueError("timeoutSeconds must be an integer in 1..900")
    return argv, env, timeout, adapter, transport


def extract_structured(adapter, raw):
    text = raw.strip()
    if not text:
        return None, "empty-output"
    if adapter == "raw":
        return {"format": "raw", "characters": len(text)}, None
    if adapter in ("codex", "opencode"):
        values = []
        for line in text.splitlines():
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not values:
            return None, "invalid-structured-output"
        if adapter == "codex":
            messages = [v.get("item", {}).get("text") for v in values
                        if isinstance(v, dict) and v.get("type") == "item.completed"
                        and isinstance(v.get("item"), dict)
                        and v["item"].get("type") == "agent_message"]
        else:
            messages = [v.get("part", {}).get("text") for v in values
                        if isinstance(v, dict) and v.get("type") == "text"
                        and isinstance(v.get("part"), dict)
                        and v["part"].get("type") == "text"]
        messages = [m for m in messages if isinstance(m, str) and m.strip()]
        if not messages:
            return None, "missing-review-content"
        return {"format": "jsonl", "events": len(values), "reviewText": "\n".join(messages)}, None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid-structured-output"
    if adapter == "claude":
        result = value.get("result") if isinstance(value, dict) else None
        if not isinstance(result, str) or not result.strip():
            return None, "missing-review-content"
        return {"format": "json", "reviewText": result}, None
    if adapter.startswith("cursor-json:"):
        for key in adapter.split(":", 1)[1].split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if not isinstance(value, str) or not value.strip():
            return None, "missing-review-content"
        return {"format": "json", "reviewText": value}, None
    return {"format": "json", "response": value}, None


def _drain(stream, target):
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        target["total"] += len(chunk)
        if len(target["data"]) < MAX_OUTPUT_BYTES:
            target["data"] += chunk[:MAX_OUTPUT_BYTES - len(target["data"])]


def run_one(work, cwd, run_id, seat, events):
    name = seat["name"]
    started = time.monotonic()
    base = {"seat": name, "kind": seat["kind"], "status": "", "elapsedSeconds": 0.0}
    try:
        argv, extra_env, timeout, adapter, transport = prepare_command(seat, seat["tier"], seat["prompt"])
    except ValueError as e:
        base.update(status="invalid-seat-config", reason=str(e))
        events.emit("seat-finished", seat=name, status=base["status"])
        return base

    stdout_path = work / f"out-{name}.stdout.txt"
    stderr_path = work / f"out-{name}.stderr.txt"
    env = dict(os.environ)
    env.update(extra_env)
    env["LEOS_COUNCIL_SEAT"] = "1"
    env["LEOS_COUNCIL_ACTIVE_RUN"] = run_id
    stdin_handle = open(seat["promptPath"], "rb") if transport == "stdin" else subprocess.DEVNULL
    opened_stdin = transport == "stdin"
    out, err = {"data": b"", "total": 0}, {"data": b"", "total": 0}
    try:
        events.emit("seat-started", seat=name, timeoutSeconds=timeout, adapter=adapter)
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=stdin_handle, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        t_out = threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True)
        t_out.start(); t_err.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        t_out.join(timeout=5); t_err.join(timeout=5)
        write_private(stdout_path, out["data"], binary=True)
        write_private(stderr_path, err["data"], binary=True)
        base.update({
            "argv": argv, "exitCode": proc.returncode, "timeoutSeconds": timeout,
            "stdoutPath": str(stdout_path), "stderrPath": str(stderr_path),
            "stdoutBytes": out["total"], "stderrBytes": err["total"],
            "outputTruncated": out["total"] > len(out["data"]) or err["total"] > len(err["data"]),
        })
        if timed_out:
            base["status"] = "timed-out"
        elif proc.returncode != 0:
            base["status"] = "nonzero-exit"
        else:
            parsed, failure = extract_structured(adapter, out["data"].decode("utf-8", "replace"))
            base["status"] = failure or "completed"
            if parsed:
                base["transportResult"] = parsed
    except FileNotFoundError:
        base.update(status="unavailable", reason=f"command not found: {argv[0]}")
    except OSError as e:
        base.update(status="execution-error", reason=str(e))
    finally:
        if opened_stdin:
            stdin_handle.close()
    base["elapsedSeconds"] = round(time.monotonic() - started, 3)
    events.emit("seat-finished", seat=name, status=base["status"], elapsedSeconds=base["elapsedSeconds"])
    return base


def select_seats(config, checkpoint, tier, include_native):
    external = config.get("seats", [])
    if not isinstance(external, list):
        raise ValueError("seats must be an array")
    chosen = []
    # A plan checkpoint is deliberately external-first: one strong independent reviewer normally,
    # two on high-stakes plans. The native fallback is used only when no external transport exists.
    plan_external = 0
    if checkpoint == "plan" and external:
        plan_external = 2 if tier in ("high", "critical") else 1
    native_passes = 1 if checkpoint == "plan" else (
        {"low": 1, "elevated": 2, "high": 3, "critical": 3}[tier] if not external else 1)
    if include_native and not plan_external:
        native = config.get("native")
        if not isinstance(native, dict):
            raise ValueError("native seat missing")
        if native.get("mode") == "subagent":
            for index in range(native_passes):
                chosen.append({"name": "native" if index == 0 else f"native-{index + 1}", "kind": "native",
                               "mode": "subagent", "model": native.get("model", ""),
                               "status": "orchestrator-native-subagent-required"})
        elif native.get("mode") == "exec":
            for index in range(native_passes):
                chosen.append(dict(native, name="native" if index == 0 else f"native-{index + 1}",
                                   kind="native", mode="exec"))
        else:
            raise ValueError("native mode must be subagent or exec")
    wanted = plan_external or tier_external_count(tier, len(external))
    for i, item in enumerate(external[:wanted]):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError(f"invalid external seat at index {i}")
        chosen.append(dict(item, kind="external", mode="exec"))
    return chosen


def cmd_run(args):
    if os.environ.get("LEOS_COUNCIL_SEAT"):
        print(json.dumps({"ok": False, "status": "nested-leos-council-refused",
                          "reason": "a council seat may use ordinary subagents but may not convene Leo's Agents council"}, indent=2))
        return 3
    cwd = project_root(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        print(json.dumps({"ok": False, "status": "invalid-cwd"}, indent=2))
        return 2
    prompt_source = Path(args.prompt).expanduser()
    try:
        prompt = prompt_source.read_text(encoding="utf-8")
    except OSError as e:
        print(json.dumps({"ok": False, "status": "prompt-unreadable", "reason": str(e)}, indent=2))
        return 2
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        print(json.dumps({"ok": False, "status": "prompt-too-large", "limitBytes": MAX_PROMPT_BYTES}, indent=2))
        return 2
    if SENSITIVE_PROMPT_RE.search(prompt) and not args.allow_sensitive:
        print(json.dumps({"ok": False, "status": "sensitive-prompt-refused",
                          "reason": "probable credential material was not sent to external CLI seats"}, indent=2))
        return 2
    config = read_json(LOCAL / f"seats.{args.host}.json")
    if not isinstance(config, dict):
        print(json.dumps({"ok": False, "status": "seats-unavailable", "host": args.host}, indent=2))
        return 2
    try:
        selected = select_seats(config, args.checkpoint, args.tier, not args.external_only)
    except ValueError as e:
        print(json.dumps({"ok": False, "status": "invalid-seats", "reason": str(e)}, indent=2))
        return 2

    run_id = args.run_id or secrets.token_hex(12)
    active = fresh_active_run(cwd)
    if active and active.get("run_id") != run_id:
        print(json.dumps({"ok": False, "status": "nested-leos-council-refused", "activeRun": active.get("run_id", ""),
                          "reason": "an active Leo council marker already owns this checkpoint"}, indent=2))
        return 3

    work = WORK_ROOT / project_slug(cwd) / run_id
    secure_dir(work)
    events = EventLog(work / "events.jsonl")
    prompt_path = work / f"prompt-{args.checkpoint}.md"
    write_private(prompt_path, prompt)
    begin = subprocess.run([str(PYTHON), str(COUNCIL), "begin", "--checkpoint", args.checkpoint,
                            "--run-id", run_id], cwd=cwd, text=True, capture_output=True)
    if begin.returncode != 0:
        print(json.dumps({"ok": False, "status": "begin-failed", "stderr": begin.stderr[-1000:]}, indent=2))
        return 2
    events.emit("runner-started", runId=run_id, host=args.host, checkpoint=args.checkpoint, tier=args.tier)

    planned, manual_native = [], []
    for seat in selected:
        if seat.get("mode") == "subagent":
            manual_native.append({"seat": "native", "status": "orchestrator-native-subagent-required",
                                  "model": seat.get("model", ""), "promptPath": str(prompt_path),
                                  "instruction": "Dispatch one native review subagent; do not ask it to convene Leo's Agents council."})
            continue
        planned.append(dict(seat, tier=args.tier, prompt=prompt, promptPath=str(prompt_path)))
    job = {"schema": 1, "runId": run_id, "host": args.host, "checkpoint": args.checkpoint,
           "tier": args.tier, "cwd": cwd, "promptPath": str(prompt_path), "startedAt": int(time.time()),
           "seats": [{"name": s["name"], "kind": s["kind"]} for s in planned], "manualNative": manual_native}
    write_json(work / "job.json", job)
    results = list(manual_native)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(planned))) as pool:
        futures = [pool.submit(run_one, work, cwd, run_id, seat, events) for seat in planned]
        for future in futures:
            results.append(future.result())
    summary = dict(job, finishedAt=int(time.time()), results=results)
    summary["dispatchOk"] = all(r["status"] in ("completed", "orchestrator-native-subagent-required") for r in results)
    summary["reviewComplete"] = all(r["status"] == "completed" for r in results)
    summary["requiresOrchestratorNative"] = bool(manual_native)
    # Exit 0 means every executable CLI dispatch succeeded.  It does *not* mean the review is
    # complete when a host-native subagent still needs the orchestrator to run it.
    summary["ok"] = summary["dispatchOk"]
    summary["resultPath"] = str(work / "result.json")
    write_json(work / "result.json", summary)
    events.emit("runner-finished", runId=run_id, dispatchOk=summary["dispatchOk"],
                reviewComplete=summary["reviewComplete"])
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


def main():
    ap = argparse.ArgumentParser(prog="runner.py")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="explicitly run configured external CLI seats for one council job")
    p.add_argument("--host", required=True, choices=HOSTS)
    p.add_argument("--checkpoint", required=True, choices=("impl", "plan"))
    p.add_argument("--tier", required=True, choices=TIERS)
    p.add_argument("--prompt", required=True, help="orchestrator-created review prompt file")
    p.add_argument("--cwd", help="repository being reviewed (default: current directory)")
    p.add_argument("--external-only", action="store_true", help="do not run/report the native seat")
    p.add_argument("--run-id", help="resume only the matching active runner job")
    p.add_argument("--allow-sensitive", action="store_true", help="explicitly permit a prompt matched as probable credential material")
    p.set_defaults(fn=cmd_run)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
