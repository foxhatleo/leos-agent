#!/usr/bin/env python3
"""Validate and atomically install a machine-local council seat configuration.

Model discovery and paid driver smoke tests remain explicit setup interview steps. This command
turns their resolved output into a schema-checked, private file; it never guesses model slugs or
stores credentials. The schema is a unified `seats` array: every reviewer (including the host's
own-provider seat) is one element with `mode` in {subagent, exec}, a `minTier` (1..4), and an
optional `envFile` (the per-seat secret channel). There is no top-level `native` object.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOCAL = Path(os.environ.get("LEOS_LOCAL", ROOT / "local"))
HOSTS = ("claude", "codex", "opencode", "cursor")
SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|API[_-]?KEY|PRIVATE[_-]?KEY)", re.IGNORECASE)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_config(path):
    with open(path, encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError("seat configuration must be a JSON object")
    return value


def validate(config, host):
    problems = []
    if config.get("host") != host:
        problems.append(f"host must be exactly {host!r}")
    doctor = load_module("leos_doctor_seats", ROOT / "bin" / "leos-doctor.py")
    problems.extend(doctor.check_seat_flags(host, config))
    runner = load_module("leos_runner_seats", ROOT / "core" / "council" / "bin" / "runner.py")
    seats = list(config.get("seats", []))
    for seat in seats:
        if not isinstance(seat, dict):
            continue
        name = seat.get("name", "<unnamed>")
        mode = seat.get("mode")
        # The inline `env` dict is the NON-SECRET channel — secret-named keys are refused here.
        # Secrets go in the per-seat envFile (secret-named keys allowed there, not here).
        env = seat.get("env", {})
        if isinstance(env, dict) and any(SECRET_KEY_RE.search(str(key)) for key in env):
            problems.append(f"seat {name} env contains a secret-like key")
        if mode == "subagent":
            # subagent seats are orchestrator-owned; no CLI smoke, no argv resolution. Validate
            # minTier bounds (runner.seat_min_tier raises on bad values) and the model/Opus-line
            # rule (doctor already checked); nothing to prepare_command.
            try:
                runner.seat_min_tier(seat)
            except ValueError as exc:
                problems.append(f"seat {name}: {exc}")
            continue
        if mode != "exec":
            continue  # doctor already flagged an invalid mode
        try:
            runner.prepare_command(seat, "critical", 'review {"schema": true}')
        except ValueError as exc:
            problems.append(f"seat {name}: {exc}")
    return list(dict.fromkeys(problems))


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".seats-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def main():
    ap = argparse.ArgumentParser(prog="leos-seats.py")
    ap.add_argument("command", choices=("validate", "write"))
    ap.add_argument("--host", required=True, choices=HOSTS)
    ap.add_argument("--input", required=True, help="resolved candidate JSON outside tracked files")
    ap.add_argument("--confirm-smoke", action="append", default=[], metavar="SEAT",
                    help="exec seat whose documented driver smoke test passed (repeat per seat)")
    args = ap.parse_args()
    try:
        config = read_config(args.input)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "problems": [str(exc)]}, indent=2))
        return 1
    problems = validate(config, args.host)
    # Only mode:exec seats invoke an external CLI and require a confirmed driver smoke. A
    # mode:subagent seat is orchestrator-owned (in-process host subagent) and is not smoke-gated.
    exec_names = {seat.get("name") for seat in config.get("seats", [])
                  if isinstance(seat, dict) and seat.get("mode") == "exec"}
    all_names = {seat.get("name") for seat in config.get("seats", []) if isinstance(seat, dict)}
    if args.command == "write":
        missing_smokes = exec_names - set(args.confirm_smoke)
        unknown_smokes = set(args.confirm_smoke) - all_names
        if missing_smokes:
            problems.append("missing passed driver smoke confirmation for: " + ", ".join(sorted(missing_smokes)))
        if unknown_smokes:
            problems.append("unknown smoke confirmation for: " + ", ".join(sorted(unknown_smokes)))
    if problems:
        print(json.dumps({"ok": False, "problems": problems}, indent=2))
        return 1
    if args.command == "write":
        config["smokeTests"] = {name: {"passed": True, "confirmedAt": int(time.time())}
                                for name in sorted(exec_names)}
        destination = LOCAL / f"seats.{args.host}.json"
        atomic_write(destination, config)
        print(json.dumps({"ok": True, "path": str(destination), "seats": sorted(all_names)}, indent=2))
    else:
        print(json.dumps({"ok": True, "host": args.host, "seats": sorted(all_names)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
