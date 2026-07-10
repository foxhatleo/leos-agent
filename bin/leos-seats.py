#!/usr/bin/env python3
"""Validate and atomically install a machine-local council seat configuration.

Model discovery and paid driver smoke tests remain explicit setup interview steps. This command
turns their resolved output into a schema-checked, private file; it never guesses model slugs or
stores credentials.
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
    native = config.get("native", {})
    if isinstance(native, dict) and native.get("mode") == "exec":
        seats.append(dict(native, name="native"))
    for seat in seats:
        if not isinstance(seat, dict):
            continue
        env = seat.get("env", {})
        if isinstance(env, dict) and any(SECRET_KEY_RE.search(str(key)) for key in env):
            problems.append(f"seat {seat.get('name', '<unnamed>')} env contains a secret-like key")
        try:
            runner.prepare_command(seat, "critical", 'review {"schema": true}')
        except ValueError as exc:
            problems.append(f"seat {seat.get('name', '<unnamed>')}: {exc}")
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
                    help="external seat whose documented driver smoke test passed (repeat per seat)")
    args = ap.parse_args()
    try:
        config = read_config(args.input)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "problems": [str(exc)]}, indent=2))
        return 1
    problems = validate(config, args.host)
    external_names = {seat.get("name") for seat in config.get("seats", []) if isinstance(seat, dict)}
    if args.command == "write":
        missing_smokes = external_names - set(args.confirm_smoke)
        unknown_smokes = set(args.confirm_smoke) - external_names
        if missing_smokes:
            problems.append("missing passed driver smoke confirmation for: " + ", ".join(sorted(missing_smokes)))
        if unknown_smokes:
            problems.append("unknown smoke confirmation for: " + ", ".join(sorted(unknown_smokes)))
    if problems:
        print(json.dumps({"ok": False, "problems": problems}, indent=2))
        return 1
    if args.command == "write":
        config["smokeTests"] = {name: {"passed": True, "confirmedAt": int(time.time())}
                                for name in sorted(external_names)}
        destination = LOCAL / f"seats.{args.host}.json"
        atomic_write(destination, config)
        print(json.dumps({"ok": True, "path": str(destination), "seats": sorted(external_names)}, indent=2))
    else:
        print(json.dumps({"ok": True, "host": args.host, "seats": sorted(external_names)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
