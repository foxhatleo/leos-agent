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
import shutil
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
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ACTIVE_PROCESSES = set()
ACTIVE_LOCK = threading.Lock()
CANCELLED = threading.Event()
CANCEL_REQUEST_PATH = None

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


def prepare_scratch_root(path):
    """Create a private, empty worktree that is also its own Git project root.

    Merely placing a scratch directory under ``local/`` is insufficient when this repository
    reviews itself: the parent clone would still be the discovered project root, giving its
    AGENTS.md/.cursor/rules/OpenCode config instruction authority inside the reviewer.  A nested,
    template-free Git repository preserves the local-only runtime invariant while establishing a
    hard project-discovery boundary.  Refuse dispatch if that boundary cannot be created.
    """
    secure_dir(path)
    git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    git_env.pop("GIT_TEMPLATE_DIR", None)
    # An inherited GIT_DIR/GIT_WORK_TREE would redirect the `git init` outside this private
    # scratch dir; drop them so the isolation boundary is the path argument, not the caller's repo.
    git_env.pop("GIT_DIR", None)
    git_env.pop("GIT_WORK_TREE", None)
    try:
        result = subprocess.run(
            ["git", "init", "--quiet", "--template=", str(path)],
            cwd=str(path), env=git_env, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot create isolated scratch project root: {exc}") from exc
    if result.returncode != 0:
        reason = (result.stderr or result.stdout).strip()[-500:] or f"git init exited {result.returncode}"
        raise RuntimeError(f"cannot create isolated scratch project root: {reason}")


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


def valid_run_id(value):
    return isinstance(value, str) and RUN_ID_RE.fullmatch(value) is not None


def run_work(cwd, run_id, follow_up=False):
    base = WORK_ROOT / project_slug(cwd) / run_id
    return base / "pass-2" if follow_up else base


def follow_up_preflight(cwd, args):
    """Shared follow-up preconditions for cmd_start and cmd_run. Returns
    (active, error_dict, exit_code): on success error_dict is None; for a follow-up,
    active is the fresh marker the pass will reuse. The five typed refusals:
    no-active-run, checkpoint-mismatch, run-mismatch, without-first-pass, passes-exhausted.
    Running these at `start` (not only in the detached child) prevents orphan pass-2/ dirs
    and a run id consumed by a launch that was always going to fail."""
    active = fresh_active_run(cwd)
    if not args.follow_up:
        return active, None, None
    if not active:
        return None, {"ok": False, "status": "no-active-run-for-follow-up",
                      "reason": "a follow-up pass needs the original run's fresh marker; dispatch a new run instead"}, 2
    if active.get("checkpoint") != args.checkpoint:
        return active, {"ok": False, "status": "follow-up-checkpoint-mismatch",
                        "activeCheckpoint": active.get("checkpoint", "")}, 2
    if args.run_id and args.run_id != active.get("run_id"):
        return active, {"ok": False, "status": "follow-up-run-mismatch",
                        "activeRun": active.get("run_id", "")}, 2
    base_work = WORK_ROOT / project_slug(cwd) / active.get("run_id", "")
    if not (base_work / "result.json").is_file():
        return active, {"ok": False, "status": "follow-up-without-first-pass",
                        "reason": "no completed first pass exists for the active run"}, 2
    if (base_work / "pass-2" / "result.json").is_file():
        return active, {"ok": False, "status": "follow-up-passes-exhausted",
                        "reason": "maximum two total passes"}, 2
    return active, None, None


def resolve_follow_up(cwd, run_id, follow_up_flag):
    """Pick the work-dir pass for status/stop. When the caller did NOT pass --follow-up,
    auto-detect a dispatched pass-2 so `stop --run-id R` and `status --run-id R` target pass-2
    even when the caller forgot the flag — the SKILL.md stop example documents exactly that bare
    form. --follow-up remains an explicit override. Detection keys on ANY pass-2 scaffolding
    (reservation.json is written before Popen, launcher.json after), so the bare form also covers
    the window between mkdir and launcher.json being written."""
    if follow_up_flag:
        return True
    pass2 = run_work(cwd, run_id, False) / "pass-2"
    return any((pass2 / name).is_file() for name in ("launcher.json", "reservation.json", "job.json"))


def read_events(path, limit=20):
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except OSError:
        pass
    return records[-limit:]


def process_alive(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def validate_timeout_fields(config):
    seats = config.get("seats", [])
    if not isinstance(seats, list):
        raise ValueError("seats must be an array")
    native = config.get("native")
    candidates = list(seats) + ([native] if isinstance(native, dict) else [])
    for index, seat in enumerate(candidates):
        if not isinstance(seat, dict):
            raise ValueError(f"invalid seat at index {index}")
        timeout = seat.get("timeoutSeconds", 300)
        plan_timeout = seat.get("planTimeoutSeconds")
        label = seat.get("name") or ("native" if seat is native else str(index))
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 900:
            raise ValueError(f"seat {label} timeoutSeconds must be an integer in 1..900")
        if plan_timeout is not None and (not isinstance(plan_timeout, int) or
                                         isinstance(plan_timeout, bool) or not 1 <= plan_timeout <= 900):
            raise ValueError(f"seat {label} planTimeoutSeconds must be an integer in 1..900")


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
            try:
                sys.stderr.write(f"[council-runner] {event}{suffix}\n")
                sys.stderr.flush()
            except (OSError, ValueError):
                # A dead orchestrator pipe must never kill the run; events.jsonl above is the
                # authoritative record.
                pass


def tier_external_count(tier, available):
    return {"low": 0, "elevated": 1, "high": 2, "critical": available}[tier]


def substitute(value, substitutions):
    if not isinstance(value, str):
        return value
    for needle, replacement in substitutions.items():
        value = value.replace(needle, replacement)
    return value


# Every adapter with a verified output contract. "raw" is EXPLICIT-ONLY: an unknown binary is an
# invalid seat, never silently raw — the raw path still enforces the findings contract, but a
# typo'd adapter must not pick a parser by accident.
ALLOWED_ADAPTERS = ("claude", "codex", "opencode", "cursor-json", "raw")


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
    return None


def insert_before_prompt(argv, flags):
    if argv and argv[-1] == "-":
        return argv[:-1] + flags + ["-"]
    return argv + flags


def prepare_command(seat, tier, prompt):
    """Resolve a machine-local seat into a direct CLI argv plus output contract."""
    efforts = seat.get("efforts") if isinstance(seat.get("efforts"), dict) else {}
    effort = efforts.get("max" if tier == "critical" else "default", "high")
    substitutions = {"{EFFORT}": str(effort)}
    argv_template = seat.get("argv", [])
    argv = [substitute(v, substitutions) for v in argv_template]
    if not argv or any(not isinstance(v, str) or not v for v in argv):
        raise ValueError("seat argv must be a nonempty string array")
    transport = seat.get("transport")
    if transport not in ("stdin", "arg"):
        raise ValueError("seat transport must be stdin or arg")
    allowed_placeholders = {"{PROMPT_TEXT}"} if transport == "arg" else set()
    unresolved = []
    for value in argv:
        unresolved.extend(token for token in re.findall(r"\{[A-Z][A-Z0-9_]*\}", value)
                          if token not in allowed_placeholders)
    if unresolved:
        raise ValueError("unresolved placeholder in seat argv: " + ", ".join(sorted(set(unresolved))))
    if transport == "arg":
        if "{PROMPT_TEXT}" not in argv:
            raise ValueError("arg seat must include the literal {PROMPT_TEXT} placeholder")
        if len(prompt.encode("utf-8")) > MAX_ARG_PROMPT_BYTES:
            raise ValueError("arg transport prompt exceeds 128 KiB; choose a stdin-capable transport")
        argv = [prompt if v == "{PROMPT_TEXT}" else v for v in argv]
    elif "{PROMPT_TEXT}" in argv:
        raise ValueError("stdin seat must not contain {PROMPT_TEXT}")
    cwd_mode = seat.get("cwd", "scratch")
    if cwd_mode not in ("scratch", "repo"):
        raise ValueError('seat cwd must be "scratch" (default) or "repo"')
    adapter = seat.get("adapter") if isinstance(seat.get("adapter"), str) else adapter_for(argv)
    if adapter == "cursor-unverified":
        raise ValueError("Cursor seat needs an explicit adapter: cursor-json after setup validates its JSON output contract")
    if adapter not in ALLOWED_ADAPTERS:
        raise ValueError(
            f"no known adapter for {argv[0]!r} (got {adapter!r}); set \"adapter\" to one of: "
            + ", ".join(ALLOWED_ADAPTERS))
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
    plan_timeout = seat.get("planTimeoutSeconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 900:
        raise ValueError("timeoutSeconds must be an integer in 1..900")
    if plan_timeout is not None and (not isinstance(plan_timeout, int) or isinstance(plan_timeout, bool)
                                     or not 1 <= plan_timeout <= 900):
        raise ValueError("planTimeoutSeconds must be an integer in 1..900")
    if seat.get("kind") == "external" and seat.get("checkpoint") == "plan" \
            and plan_timeout is not None:
        timeout = plan_timeout
    return argv, env, timeout, adapter, transport


def extract_structured(adapter, raw):
    text = raw.strip()
    if not text:
        return None, "empty-output"
    if adapter == "raw":
        return {"format": "raw", "characters": len(text), "reviewText": text}, None
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
    # Defense in depth: prepare_command's allow-list makes this unreachable, but an unrecognized
    # adapter's output must never classify as a completed review with no findings validation.
    return None, "unsupported-adapter"


def extract_findings(review_text, checkpoint):
    """Validate the findings contract required by both committed review prompts."""
    text = review_text.strip()
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.IGNORECASE | re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    try:
        findings = json.loads(candidate)
    except json.JSONDecodeError:
        return None, "invalid-review-findings"
    if not isinstance(findings, list):
        return None, "invalid-review-findings"
    required = ("severity", "claim", "failure_scenario", "suggested_fix", "confidence")
    for finding in findings:
        if not isinstance(finding, dict) or any(key not in finding for key in required):
            return None, "invalid-review-findings"
        if finding["severity"] not in ("high", "medium", "low"):
            return None, "invalid-review-findings"
        if any(not isinstance(finding[key], str) or not finding[key].strip()
               for key in ("claim", "failure_scenario", "suggested_fix")):
            return None, "invalid-review-findings"
        confidence = finding["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                or not 0 <= confidence <= 1:
            return None, "invalid-review-findings"
        if checkpoint == "impl":
            if not isinstance(finding.get("file"), str) or not finding["file"].strip() \
                    or isinstance(finding.get("line"), bool) \
                    or not isinstance(finding.get("line"), int) or finding["line"] < 1:
                return None, "invalid-review-findings"
    return findings, None


def _drain(stream, target):
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        target["total"] += len(chunk)
        if len(target["data"]) < MAX_OUTPUT_BYTES:
            target["data"] += chunk[:MAX_OUTPUT_BYTES - len(target["data"])]


def _killpg(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def cancel_active(_signum, _frame):
    CANCELLED.set()
    # ACTIVE_LOCK also guards launch+registration in run_one, so a seat is either not yet
    # launched (its worker sees CANCELLED inside the lock and refuses) or in this snapshot.
    with ACTIVE_LOCK:
        processes = list(ACTIVE_PROCESSES)
    for proc in processes:
        _killpg(proc, signal.SIGTERM)


def _wait_bounded(proc, timeout):
    """Wait with the TERM-at-deadline / KILL-after-5s timeout contract, plus a bounded exit on
    cancellation: a cancelled run tears down within the grace window, never the full seat
    timeout. Returns timed_out."""
    deadline = time.monotonic() + timeout
    kill_at = None
    cancel_grace = None
    timed_out = False
    while proc.poll() is None:
        now = time.monotonic()
        if CANCEL_REQUEST_PATH is not None and CANCEL_REQUEST_PATH.is_file():
            CANCELLED.set()
        if CANCELLED.is_set() and cancel_grace is None:
            cancel_grace = now + 5
            _killpg(proc, signal.SIGTERM)   # idempotent; covers a pre-snapshot registration
        if cancel_grace is not None and now >= cancel_grace:
            _killpg(proc, signal.SIGKILL)
            proc.wait()
            break
        if not timed_out and now >= deadline:
            timed_out = True
            kill_at = now + 5
            _killpg(proc, signal.SIGTERM)
        elif timed_out and now >= kill_at:
            _killpg(proc, signal.SIGKILL)
            proc.wait()
            break
        time.sleep(0.05)
    return timed_out


def run_one(work, cwd, seat, events):
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
    # Default isolation: a private per-seat scratch cwd with its own synthetic Git root, so
    # repo-local agent config (.cursor/rules, AGENTS.md, OpenCode project config) never gains
    # instruction authority inside a reviewer — including when this clone reviews itself. The
    # prompt header carries the reviewed repo path; "cwd": "repo" opts out for transports that
    # cannot read outside their workspace (documented residual risk).
    cwd_mode = seat.get("cwd", "scratch")
    scratch = work / f"cwd-{name}"
    if cwd_mode == "scratch":
        try:
            prepare_scratch_root(scratch)
        except RuntimeError as exc:
            shutil.rmtree(scratch, ignore_errors=True)
            base.update(status="isolation-error", reason=str(exc))
            events.emit("seat-finished", seat=name, status=base["status"])
            return base
        seat_cwd = str(scratch)
    else:
        seat_cwd = cwd
    base["cwdMode"] = cwd_mode
    stdin_handle = open(seat["promptPath"], "rb") if transport == "stdin" else subprocess.DEVNULL
    opened_stdin = transport == "stdin"
    out, err = {"data": b"", "total": 0}, {"data": b"", "total": 0}
    proc = None
    try:
        # Launch and register atomically against the cancel handler: either the handler's
        # snapshot has this proc, or CANCELLED was already set and we refuse to launch.
        with ACTIVE_LOCK:
            if CANCELLED.is_set():
                base.update(status="cancelled", reason="cancelled-before-launch")
                events.emit("seat-finished", seat=name, status=base["status"])
                return base
            proc = subprocess.Popen(
                argv, cwd=seat_cwd, env=env, stdin=stdin_handle, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
            )
            ACTIVE_PROCESSES.add(proc)
        if CANCELLED.is_set():
            _killpg(proc, signal.SIGTERM)
        events.emit("seat-started", seat=name, timeoutSeconds=timeout, adapter=adapter,
                    pid=proc.pid)
        t_out = threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True)
        t_out.start(); t_err.start()
        timed_out = _wait_bounded(proc, timeout)
        t_out.join(timeout=5); t_err.join(timeout=5)
        write_private(stdout_path, out["data"], binary=True)
        write_private(stderr_path, err["data"], binary=True)
        # The persisted argv is for diagnostics only. For arg-transport seats it would otherwise
        # echo the full prompt (and any non-matching secret) into result.json at rest — the live
        # send already used the real argv, and the prompt is separately preserved at promptPath,
        # so redact the prompt element in this diagnostic copy.
        persist_argv = list(argv)
        if transport == "arg" and isinstance(seat.get("prompt"), str) and seat["prompt"]:
            _prompt = seat["prompt"]
            persist_argv = [("<redacted prompt %d bytes>" % len(_prompt.encode("utf-8")))
                            if v == _prompt else v for v in persist_argv]
        base.update({
            "argv": persist_argv, "exitCode": proc.returncode, "timeoutSeconds": timeout,
            "stdoutPath": str(stdout_path), "stderrPath": str(stderr_path),
            "stdoutBytes": out["total"], "stderrBytes": err["total"],
            "outputTruncated": out["total"] > len(out["data"]) or err["total"] > len(err["data"]),
        })
        # Exact classification: a seat that finished its work stays completed even if the run
        # was cancelled afterwards; cancelled only ever means "this run's cancellation stopped
        # the seat"; an externally signalled seat without cancellation/timeout is signal-exit.
        if timed_out:
            base["status"] = "timed-out"
        elif proc.returncode == 0:
            parsed, failure = extract_structured(adapter, out["data"].decode("utf-8", "replace"))
            if not failure and parsed and isinstance(parsed.get("reviewText"), str):
                findings, failure = extract_findings(parsed["reviewText"], seat["checkpoint"])
                if findings is not None:
                    parsed["findings"] = findings
            base["status"] = failure or "completed"
            if parsed:
                base["transportResult"] = parsed
        elif CANCELLED.is_set():
            base["status"] = "cancelled"
        elif proc.returncode is not None and proc.returncode < 0:
            base["status"] = "signal-exit"
        else:
            base["status"] = "nonzero-exit"
    except FileNotFoundError:
        base.update(status="unavailable", reason=f"command not found: {argv[0]}")
    except OSError as e:
        base.update(status="execution-error", reason=str(e))
    finally:
        if proc is not None:
            with ACTIVE_LOCK:
                ACTIVE_PROCESSES.discard(proc)
        if opened_stdin:
            stdin_handle.close()
        if cwd_mode == "scratch":
            shutil.rmtree(scratch, ignore_errors=True)
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
        # Seat names feed work-dir file and scratch-cwd names — keep them path-safe.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", item["name"]):
            raise ValueError(f"invalid external seat name {item['name']!r}")
        chosen.append(dict(item, kind="external", mode="exec"))
    return chosen


def native_seats(config, count=1):
    native = config.get("native")
    if not isinstance(native, dict):
        raise ValueError("native seat missing")
    if native.get("mode") not in ("subagent", "exec"):
        raise ValueError("native mode must be subagent or exec")
    seats = []
    for index in range(count):
        seat = dict(native, name="native" if index == 0 else f"native-{index + 1}", kind="native")
        if native.get("mode") == "subagent":
            # Only a subagent seat is orchestrator-owned; an exec seat is dispatched normally.
            seat["status"] = "orchestrator-native-subagent-required"
        seats.append(seat)
    return seats


def cmd_run(args):
    global CANCEL_REQUEST_PATH
    if os.environ.get("LEOS_COUNCIL_SEAT"):
        print(json.dumps({"ok": False, "status": "nested-leos-council-refused",
                          "reason": "a council seat may use ordinary subagents but may not convene Leo's Agents council"}, indent=2))
        return 3
    cwd = project_root(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        print(json.dumps({"ok": False, "status": "invalid-cwd"}, indent=2))
        return 2
    if args.run_id and not valid_run_id(args.run_id):
        print(json.dumps({"ok": False, "status": "invalid-run-id",
                          "reason": "run id must be 1..64 path-safe characters"}, indent=2))
        return 2
    prompt_source = Path(args.prompt).expanduser()
    try:
        prompt = prompt_source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(json.dumps({"ok": False, "status": "prompt-unreadable", "reason": str(e)}, indent=2))
        return 2
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        print(json.dumps({"ok": False, "status": "prompt-too-large", "limitBytes": MAX_PROMPT_BYTES}, indent=2))
        return 2
    prompt_redacted = False
    if SENSITIVE_PROMPT_RE.search(prompt):
        if not args.redact_sensitive:
            print(json.dumps({"ok": False, "status": "sensitive-prompt-refused",
                              "reason": "probable credential material requires deterministic redaction"}, indent=2))
            return 2
        # A key/file marker can cover following lines that the regex cannot safely delimit. Withhold
        # the complete prompt rather than risk leaking the remainder of a PEM/env-file diff.
        prompt = ("[REDACTED-SENSITIVE-PROMPT]\n"
                  "The review context was withheld because probable credential material was detected.\n")
        prompt_redacted = True
    # Seats launch in an isolated scratch project root by default, so the reviewed repo's
    # location must travel in the prompt itself (after redaction: the path is machine-local
    # metadata, not credential material) for read-capable transports to verify claims.
    prompt = (f"Repository under review (absolute path): {cwd}\n"
              f"Verify claims by reading files under that path; do not modify the repository.\n\n"
              + prompt)
    config = read_json(LOCAL / f"seats.{args.host}.json")
    if not isinstance(config, dict):
        print(json.dumps({"ok": False, "status": "seats-unavailable", "host": args.host}, indent=2))
        return 2
    try:
        validate_timeout_fields(config)
        selected = select_seats(config, args.checkpoint, args.tier, not args.external_only)
    except ValueError as e:
        print(json.dumps({"ok": False, "status": "invalid-seats", "reason": str(e)}, indent=2))
        return 2
    if args.seat:
        if not args.follow_up:
            print(json.dumps({"ok": False, "status": "seat-requires-follow-up",
                              "reason": "--seat selects the single re-review seat of a --follow-up pass"}, indent=2))
            return 2
        selected = [seat for seat in selected if seat.get("name") == args.seat]
        if not selected:
            print(json.dumps({"ok": False, "status": "seat-not-configured", "seat": args.seat}, indent=2))
            return 2
    if any(seat.get("kind") == "external" for seat in selected) and not args.approve_external:
        print(json.dumps({"ok": False, "status": "external-send-approval-required",
                          "reason": "confirm this project's prompt may be sent to configured external providers"}, indent=2))
        return 2

    active, err, code = follow_up_preflight(cwd, args)
    if err:
        print(json.dumps(err, indent=2))
        return code
    if args.follow_up:
        # The mandated single fix->re-review pass reuses the ACTIVE run's marker and run id and
        # writes under <run>/pass-2/, so round-1 artifacts stay immutable.
        run_id = active.get("run_id")
    else:
        run_id = args.run_id or secrets.token_hex(12)
        if active and active.get("run_id") != run_id:
            print(json.dumps({"ok": False, "status": "nested-leos-council-refused", "activeRun": active.get("run_id", ""),
                              "reason": "an active Leo council marker already owns this checkpoint"}, indent=2))
            return 3

    if not valid_run_id(run_id):
        print(json.dumps({"ok": False, "status": "invalid-run-id"}, indent=2))
        return 2
    base_work = WORK_ROOT / project_slug(cwd) / run_id
    work = base_work / "pass-2" if args.follow_up else base_work
    if args.detached_token:
        reservation = read_json(work / "reservation.json", {})
        if not isinstance(reservation, dict) or reservation.get("token") != args.detached_token \
                or reservation.get("runId") != run_id:
            print(json.dumps({"ok": False, "status": "invalid-detached-reservation"}, indent=2))
            return 2
    else:
        secure_dir(work.parent)
        try:
            work.mkdir(mode=0o700)
        except FileExistsError:
            print(json.dumps({"ok": False, "status": "run-id-work-exists",
                              "reason": "refusing to overwrite or join existing run artifacts"}, indent=2))
            return 2
    CANCEL_REQUEST_PATH = work / "cancel-request.json"
    if CANCEL_REQUEST_PATH.is_file():
        CANCELLED.set()
    events = EventLog(work / "events.jsonl")
    prompt_path = work / f"prompt-{args.checkpoint}.md"
    write_private(prompt_path, prompt)
    try:
        begin = subprocess.run([str(PYTHON), str(COUNCIL), "begin", "--checkpoint", args.checkpoint,
                                "--run-id", run_id], cwd=cwd, text=True, capture_output=True)
    except OSError as exc:
        # bin/leos-python missing / not executable / broken symlink (CLI symlinks break on app
        # updates) — emit a typed status and clean up rather than a raw traceback.
        shutil.rmtree(work, ignore_errors=True)
        print(json.dumps({"ok": False, "status": "begin-error",
                          "reason": f"could not execute council engine: {exc}"}, indent=2))
        return 2
    if begin.returncode != 0:
        if begin.returncode == 3:
            print(begin.stdout or json.dumps({"ok": False, "status": "nested-leos-council-refused"}, indent=2))
            return 3
        print(json.dumps({"ok": False, "status": "begin-failed", "stderr": begin.stderr[-1000:]}, indent=2))
        return 2
    events.emit("runner-started", runId=run_id, host=args.host, checkpoint=args.checkpoint, tier=args.tier)

    planned, manual_native = [], []
    for seat in selected:
        if seat.get("mode") == "subagent":
            manual_native.append({"seat": seat["name"], "status": "orchestrator-native-subagent-required",
                                  "model": seat.get("model", ""), "promptPath": str(prompt_path),
                                  "instruction": "Dispatch one native review subagent; do not ask it to convene Leo's Agents council."})
            continue
        planned.append(dict(seat, tier=args.tier, checkpoint=args.checkpoint,
                            prompt=prompt, promptPath=str(prompt_path)))
    job = {"schema": 1, "runId": run_id, "host": args.host, "checkpoint": args.checkpoint,
           "tier": args.tier, "cwd": cwd, "promptPath": str(prompt_path), "promptRedacted": prompt_redacted,
           "pass": 2 if args.follow_up else 1,
           "externalSendApproved": bool(args.approve_external), "startedAt": int(time.time()),
           "seats": [{"name": s["name"], "kind": s["kind"]} for s in planned], "manualNative": manual_native}
    write_json(work / "job.json", job)
    results = list(manual_native)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(planned))) as pool:
        futures = [pool.submit(run_one, work, cwd, seat, events) for seat in planned]
        for future in futures:
            results.append(future.result())
    if args.checkpoint == "plan" and not args.external_only and planned \
            and all(seat.get("kind") == "external" for seat in planned) \
            and not any(result["status"] == "completed" for result in results):
        events.emit("fallback-fired", reason="all-plan-external-seats-failed")
        try:
            fallback_seats = native_seats(config)
        except ValueError as e:
            # The plan+external selection path never validated the native block; a broken one
            # must yield a typed result (dispatchOk false -> the end path below releases the
            # marker) instead of an uncaught crash that leaks the marker for its full TTL.
            events.emit("fallback-unavailable", reason=str(e))
            fallback_seats = []
            results.append({"seat": "native", "kind": "native", "status": "invalid-seat-config",
                            "reason": f"plan fallback unavailable: {e}", "fallback": True,
                            "elapsedSeconds": 0.0})
        for seat in fallback_seats:
            if seat.get("mode") == "subagent":
                manual = {"seat": seat["name"], "status": "orchestrator-native-subagent-required",
                          "model": seat.get("model", ""), "promptPath": str(prompt_path),
                          "fallback": True,
                          "instruction": "Dispatch one native review subagent; do not ask it to convene Leo's Agents council."}
                manual_native.append(manual)
                results.append(manual)
            else:
                fallback = dict(seat, tier=args.tier, checkpoint=args.checkpoint,
                                prompt=prompt, promptPath=str(prompt_path), fallback=True)
                results.append(run_one(work, cwd, fallback, events))
    summary = dict(job, finishedAt=int(time.time()), results=results)
    summary["dispatchOk"] = bool(results) and all(
        r["status"] in ("completed", "orchestrator-native-subagent-required") for r in results)
    summary["reviewComplete"] = bool(results) and all(r["status"] == "completed" for r in results)
    summary["requiresOrchestratorNative"] = bool(manual_native)
    # Exit 0 means every executable CLI dispatch succeeded.  It does *not* mean the review is
    # complete when a host-native subagent still needs the orchestrator to run it.
    summary["ok"] = summary["dispatchOk"]
    summary["resultPath"] = str(work / "result.json")
    write_json(work / "result.json", summary)
    if not summary["dispatchOk"] and not summary["requiresOrchestratorNative"]:
        subprocess.run([str(PYTHON), str(COUNCIL), "end", "--run-id", run_id,
                        "--status", "dispatch-failed"], cwd=cwd, text=True, capture_output=True)
    events.emit("runner-finished", runId=run_id, dispatchOk=summary["dispatchOk"],
                reviewComplete=summary["reviewComplete"])
    try:
        print(json.dumps(summary, indent=2))
    except (OSError, ValueError):
        pass   # result.json is already written; a dead stdout must not change the outcome
    return 0 if summary["ok"] else 1


def dispatch_argv(args, run_id, detached_token):
    argv = [str(PYTHON), str(HERE), "run", "--host", args.host,
            "--checkpoint", args.checkpoint, "--tier", args.tier,
            "--prompt", str(Path(args.prompt).expanduser()),
            "--cwd", project_root(args.cwd or os.getcwd()), "--run-id", run_id,
            "--detached-token", detached_token]
    for flag, enabled in (("--external-only", args.external_only),
                          ("--follow-up", args.follow_up),
                          ("--approve-external", args.approve_external),
                          ("--redact-sensitive", args.redact_sensitive)):
        if enabled:
            argv.append(flag)
    if args.seat:
        argv.extend(["--seat", args.seat])
    return argv


def _read_child_status(stdout_path):
    """Best-effort recover the last JSON object a detached child printed before exiting, so
    cmd_start can surface the child's typed status (e.g. invalid-seats, begin-error) instead of
    the generic launcher-exited-without-result when the child failed before writing result.json."""
    try:
        text = Path(stdout_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # The child prints one indented JSON object; scan from the last '{' that begins a line and
    # try to parse forward. Falls back to None if nothing parses.
    for idx in range(text.rfind("{"), -1, -1):
        if idx > 0 and text[idx - 1] not in ("\n", "\r", " ", "\t"):
            continue
        try:
            value = json.loads(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def status_payload(cwd, run_id, follow_up=False):
    follow_up = resolve_follow_up(cwd, run_id, follow_up)
    work = run_work(cwd, run_id, follow_up)
    result_path = work / "result.json"
    events_path = work / "events.jsonl"
    launcher_path = work / "launcher.json"
    launcher = read_json(launcher_path, {})
    result = read_json(result_path)
    payload = {
        "ok": True,
        "runId": run_id,
        "state": "terminal" if isinstance(result, dict) else "running",
        "terminal": isinstance(result, dict),
        "resultPath": str(result_path),
        "eventsPath": str(events_path),
        "events": read_events(events_path),
    }
    if isinstance(result, dict):
        payload["result"] = result
        return payload
    pid = launcher.get("pid") if isinstance(launcher, dict) else None
    if not process_alive(pid):
        payload.update(ok=False, state="launcher-failed", terminal=True,
                       status="launcher-exited-without-result",
                       stdoutPath=str(work / "launcher.stdout.txt"),
                       stderrPath=str(work / "launcher.stderr.txt"))
    else:
        payload["pid"] = pid
    return payload


def cmd_start(args):
    if os.environ.get("LEOS_COUNCIL_SEAT"):
        print(json.dumps({"ok": False, "status": "nested-leos-council-refused"}, indent=2))
        return 3
    cwd = project_root(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        print(json.dumps({"ok": False, "status": "invalid-cwd"}, indent=2))
        return 2
    if args.follow_up:
        # Run the shared follow-up preconditions BEFORE creating pass-2/, so a bad follow-up
        # (no active marker, checkpoint drift, missing first pass, passes exhausted) returns its
        # typed status without spawning a child that would only fail inside cmd_run and leak the
        # work dir.
        active, err, code = follow_up_preflight(cwd, args)
        if err:
            print(json.dumps(err, indent=2))
            return code
        run_id = args.run_id or active.get("run_id")
    else:
        run_id = args.run_id or secrets.token_hex(12)
    if not valid_run_id(run_id):
        print(json.dumps({"ok": False, "status": "invalid-run-id",
                          "reason": "run id must be 1..64 path-safe characters"}, indent=2))
        return 2
    work = run_work(cwd, run_id, args.follow_up)
    secure_dir(work.parent)
    try:
        work.mkdir(mode=0o700)
    except FileExistsError:
        print(json.dumps({"ok": False, "status": "run-id-work-exists", "runId": run_id}, indent=2))
        return 2
    detached_token = secrets.token_hex(24)
    write_json(work / "reservation.json", {"schema": 1, "runId": run_id,
                                             "token": detached_token,
                                             "createdAt": int(time.time())})
    launcher_path = work / "launcher.json"
    stdout_path = work / "launcher.stdout.txt"
    stderr_path = work / "launcher.stderr.txt"
    out_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    err_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        proc = subprocess.Popen(dispatch_argv(args, run_id, detached_token), stdin=subprocess.DEVNULL,
                                stdout=out_fd, stderr=err_fd, start_new_session=True,
                                close_fds=True)
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        print(json.dumps({"ok": False, "status": "launcher-execution-error",
                          "reason": str(exc)}, indent=2))
        return 2
    finally:
        os.close(out_fd)
        os.close(err_fd)
    launcher = {"schema": 1, "runId": run_id, "pid": proc.pid, "startedAt": int(time.time()),
                "cwd": cwd, "followUp": bool(args.follow_up),
                "stdoutPath": str(stdout_path), "stderrPath": str(stderr_path)}
    write_json(launcher_path, launcher)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if (work / "job.json").exists() or (work / "result.json").exists() or proc.poll() is not None:
            break
        time.sleep(0.05)
    # If the child exited before writing job.json/result.json, it hit an early typed failure
    # (invalid-seats, prompt-unreadable, begin-error, a follow-up precondition the preflight
    # couldn't catch because state changed between start and the child's run). Tear down the
    # reservation so the run id isn't permanently consumed, and surface the typed status the
    # child printed instead of the generic launcher-exited-without-result.
    if proc.poll() is not None and not (work / "result.json").exists():
        child_status = _read_child_status(stdout_path)
        # Preserve the launcher logs (they live under work/) before tearing the work dir down,
        # so an operator can inspect why the detached launch failed. Stage them under the parent
        # run dir, which the reservation owns and a retry cleans up.
        log_keep = []
        for log_src, log_name in ((stdout_path, "pass-2-launcher.stdout.txt" if args.follow_up else "launcher.stdout.txt"),
                                  (stderr_path, "pass-2-launcher.stderr.txt" if args.follow_up else "launcher.stderr.txt")):
            try:
                if os.path.exists(log_src):
                    kept = work.parent / log_name
                    os.replace(log_src, kept)
                    log_keep.append(str(kept))
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)
        if child_status:
            print(json.dumps(child_status, indent=2))
            return 0 if child_status.get("ok") else 1
        print(json.dumps({"ok": False, "state": "terminal", "terminal": True,
                           "status": "launcher-exited-without-result",
                           "runId": run_id,
                           "stdoutPath": log_keep[0] if len(log_keep) > 0 else str(stdout_path),
                           "stderrPath": log_keep[1] if len(log_keep) > 1 else str(stderr_path)},
                          indent=2))
        return 1
    payload = status_payload(cwd, run_id, args.follow_up)
    payload.update(started=payload.get("state") == "running", launcherPath=str(launcher_path),
                   stdoutPath=str(stdout_path), stderrPath=str(stderr_path))
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def cmd_status(args):
    cwd = project_root(args.cwd or os.getcwd())
    if not valid_run_id(args.run_id):
        print(json.dumps({"ok": False, "status": "invalid-run-id"}, indent=2))
        return 2
    follow_up = resolve_follow_up(cwd, args.run_id, args.follow_up)
    payload = status_payload(cwd, args.run_id, follow_up)
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def cmd_stop(args):
    cwd = project_root(args.cwd or os.getcwd())
    if not valid_run_id(args.run_id):
        print(json.dumps({"ok": False, "status": "invalid-run-id"}, indent=2))
        return 2
    # Resolve pass-2 from launcher.json when the caller omitted --follow-up, so the bare
    # `stop --run-id R --cwd $PWD` form documented in SKILL.md cancels a running follow-up
    # (writing cancel-request.json to the pass-2 dir the child actually polls).
    follow_up = resolve_follow_up(cwd, args.run_id, args.follow_up)
    work = run_work(cwd, args.run_id, follow_up)
    existing = read_json(work / "result.json")
    if isinstance(existing, dict):
        payload = status_payload(cwd, args.run_id, follow_up)
        payload.update(stopRequested=False, status="already-terminal")
        print(json.dumps(payload, indent=2))
        return 0
    active = fresh_active_run(cwd)
    if not active or active.get("run_id") != args.run_id:
        print(json.dumps({"ok": False, "status": "run-not-active", "runId": args.run_id}, indent=2))
        return 2
    write_json(work / "cancel-request.json", {"schema": 1, "runId": args.run_id,
                                               "requestedAt": int(time.time())})
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline and not (work / "result.json").exists():
        time.sleep(0.05)
    payload = status_payload(cwd, args.run_id, follow_up)
    payload["stopRequested"] = True
    print(json.dumps(payload, indent=2))
    return 0


def cmd_collect_native(args):
    result_path = Path(args.result).expanduser().resolve()
    work_root = WORK_ROOT.resolve()
    if result_path.parent == work_root or work_root not in result_path.parents:
        print(json.dumps({"ok": False, "status": "invalid-result-path"}, indent=2))
        return 2
    summary = read_json(result_path)
    if not isinstance(summary, dict) or summary.get("resultPath") != str(result_path):
        print(json.dumps({"ok": False, "status": "invalid-result-file"}, indent=2))
        return 2
    try:
        review_text = Path(args.review_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(json.dumps({"ok": False, "status": "review-unreadable", "reason": str(exc)}, indent=2))
        return 2
    if len(review_text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        print(json.dumps({"ok": False, "status": "review-too-large"}, indent=2))
        return 2
    findings, failure = extract_findings(review_text, summary.get("checkpoint"))
    if failure:
        print(json.dumps({"ok": False, "status": failure}, indent=2))
        return 2
    matches = [item for item in summary.get("results", [])
               if item.get("seat") == args.seat
               and item.get("status") == "orchestrator-native-subagent-required"]
    if len(matches) != 1:
        print(json.dumps({"ok": False, "status": "native-seat-not-pending", "seat": args.seat}, indent=2))
        return 2
    matches[0].update(status="completed", transportResult={"format": "collected-native",
                      "reviewText": review_text, "findings": findings})
    summary["reviewComplete"] = bool(summary["results"]) and all(
        item.get("status") == "completed" for item in summary["results"])
    summary["requiresOrchestratorNative"] = any(
        item.get("status") == "orchestrator-native-subagent-required" for item in summary["results"])
    summary["dispatchOk"] = all(item.get("status") == "completed" or
                                item.get("status") == "orchestrator-native-subagent-required"
                                for item in summary["results"])
    summary["ok"] = summary["dispatchOk"]
    write_json(result_path, summary)
    print(json.dumps(summary, indent=2))
    return 0


def add_dispatch_arguments(parser):
    parser.add_argument("--host", required=True, choices=HOSTS)
    parser.add_argument("--checkpoint", required=True, choices=("impl", "plan"))
    parser.add_argument("--tier", required=True, choices=TIERS)
    parser.add_argument("--prompt", required=True, help="orchestrator-created review prompt file")
    parser.add_argument("--cwd", help="repository being reviewed (default: current directory)")
    parser.add_argument("--external-only", action="store_true", help="do not run/report the native seat")
    parser.add_argument("--run-id", help="path-safe id for this runner job")
    parser.add_argument("--follow-up", action="store_true",
                        help="dispatch the single re-review pass under the active run's marker (writes <run>/pass-2/)")
    parser.add_argument("--seat", help="with --follow-up: dispatch exactly this configured seat")
    parser.add_argument("--approve-external", action="store_true",
                        help="explicitly approve sending this project prompt to configured external providers")
    parser.add_argument("--redact-sensitive", action="store_true",
                        help="replace probable credential material before dispatch; raw matched content is never sent")
    parser.add_argument("--detached-token", help=argparse.SUPPRESS)


def add_run_reference_arguments(parser):
    parser.add_argument("--run-id", required=True, help="path-safe runner job id")
    parser.add_argument("--cwd", help="repository being reviewed (default: current directory)")
    parser.add_argument("--follow-up", action="store_true", help="target this run's pass-2 work directory")


def main():
    signal.signal(signal.SIGINT, cancel_active)
    signal.signal(signal.SIGTERM, cancel_active)
    ap = argparse.ArgumentParser(prog="runner.py")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="explicitly run configured external CLI seats for one council job")
    add_dispatch_arguments(p)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("start", help="detach a council job and return a pollable run id")
    add_dispatch_arguments(p)
    p.set_defaults(fn=cmd_start)
    p = sub.add_parser("status", help="poll a detached council job")
    add_run_reference_arguments(p)
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("stop", help="cancel a detached council job and its seat process groups")
    add_run_reference_arguments(p)
    p.set_defaults(fn=cmd_stop)
    p = sub.add_parser("collect-native", help="collect one orchestrator-run native subagent result")
    p.add_argument("--result", required=True, help="runner result.json path")
    p.add_argument("--seat", required=True, help="pending native seat name")
    p.add_argument("--review-file", required=True, help="native subagent findings JSON/fenced JSON")
    p.set_defaults(fn=cmd_collect_native)
    args = ap.parse_args()
    return args.fn(args)


def _exit(code):
    """A dead orchestrator pipe must not turn the runner's exit code into CPython's 120
    (failed shutdown flush): point any broken std stream at devnull before exiting."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, stream.fileno())
                os.close(devnull)
            except (OSError, ValueError):
                pass
    sys.exit(code)


if __name__ == "__main__":
    _exit(main())
