#!/usr/bin/env python3
"""Bootstrap and inspect Leo's Agents' private Python runtime.

The only system-level prerequisite is a CPython 3.9+ interpreter with ``venv`` available.  It is
used solely to create ``local/.venv``.  Every normal Leo entry point is then launched through
``bin/leos-python`` and never depends on the ambient ``python3`` selected by a host hook.

This script intentionally does *not* re-exec into the venv: it is the recovery path when the venv
does not exist or needs refreshing.
"""

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
LOCAL = Path(os.environ.get("LEOS_LOCAL", ROOT / "local"))
VENV = LOCAL / ".venv"
VENV_PYTHON = VENV / "bin" / "python"
REQUIREMENTS = ROOT / "requirements" / "runtime.txt"
STATE = LOCAL / "runtime-state.json"
MIN_PYTHON = (3, 9)


def secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def atomic_json(path: Path, data: dict) -> None:
    secure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
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


def requirements_sha() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def version_of(executable: str):
    try:
        r = subprocess.run(
            [executable, "-c", "import json, sys; print(json.dumps(list(sys.version_info[:3])))"],
            text=True, capture_output=True, timeout=10,
        )
        value = json.loads(r.stdout) if r.returncode == 0 else None
        if isinstance(value, list) and len(value) == 3 and all(isinstance(x, int) for x in value):
            return tuple(value)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        pass
    return None


def choose_bootstrap(explicit: Optional[str]):
    """Return the first executable CPython >= 3.9, without installing anything globally."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    elif os.environ.get("LEOS_PYTHON"):
        candidates.append(os.environ["LEOS_PYTHON"])
    candidates.extend([f"python3.{n}" for n in range(14, 8, -1)])
    candidates.extend(["python3", "python"])
    seen = set()
    for candidate in candidates:
        path = candidate if os.path.sep in candidate else shutil.which(candidate)
        if not path or os.path.realpath(path) in seen:
            continue
        seen.add(os.path.realpath(path))
        version = version_of(path)
        if version and version >= MIN_PYTHON:
            return path, version
    return None, None


def status() -> dict:
    version = version_of(str(VENV_PYTHON)) if VENV_PYTHON.is_file() else None
    expected = requirements_sha() if REQUIREMENTS.is_file() else None
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    import_ok = pip_ok = False
    if version and version >= MIN_PYTHON:
        try:
            probe = subprocess.run([str(VENV_PYTHON), "-c", "import tomli"],
                                   capture_output=True, text=True, timeout=15)
            import_ok = probe.returncode == 0
            check = subprocess.run([str(VENV_PYTHON), "-m", "pip", "check"],
                                   capture_output=True, text=True, timeout=30)
            pip_ok = check.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    interpreter_ok = os.path.realpath(state.get("venvPython", "")) == os.path.realpath(VENV_PYTHON)
    return {
        "ok": bool(version and version >= MIN_PYTHON and expected
                   and state.get("requirementsSha") == expected and interpreter_ok
                   and import_ok and pip_ok),
        "venv": str(VENV),
        "python": str(VENV_PYTHON),
        "pythonVersion": list(version) if version else None,
        "requirementsSha": expected,
        "installedRequirementsSha": state.get("requirementsSha"),
        "interpreterStateMatches": interpreter_ok,
        "requiredImportsOk": import_ok,
        "pipCheckOk": pip_ok,
        "refreshCommand": f"python3 {ROOT}/bin/leos-runtime.py setup --refresh",
    }


def setup(args) -> int:
    if not REQUIREMENTS.is_file():
        print(f"runtime requirements missing: {REQUIREMENTS}", file=sys.stderr)
        return 1
    secure_dir(LOCAL)
    # `setup --refresh` is the normal post-pull command. If the lock is unchanged and the private
    # runtime is healthy, it must be an offline no-op rather than making every upgrade depend on a
    # package-index lookup. An unhealthy/stale runtime is rebuilt beside the live one and swapped
    # only after the locked dependency install succeeds.
    existing = status()
    if existing["ok"]:
        print(json.dumps(existing, indent=2))
        return 0
    bootstrap, bootstrap_version = choose_bootstrap(args.python)
    if not bootstrap:
        print("Leo's Agents needs an approved CPython 3.9+ with venv to rebuild local/.venv. "
              "Select one with LEOS_PYTHON or --python; no system package manager was run.",
              file=sys.stderr)
        return 1
    staging = LOCAL / f".venv-staging-{os.getpid()}"
    old = LOCAL / f".venv-old-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True); shutil.rmtree(old, ignore_errors=True)
    r = subprocess.run([bootstrap, "-m", "venv", str(staging)], text=True)
    staging_python = staging / "bin" / "python"
    if r.returncode != 0 or not staging_python.is_file():
        shutil.rmtree(staging, ignore_errors=True)
        print("Could not create the staged private runtime; the live runtime was untouched.", file=sys.stderr)
        return 1
    cmd = [str(staging_python), "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes",
           "--no-input", "--upgrade", "-r", str(REQUIREMENTS)]
    # Keep pip's cache/build scratch inside local/ too.  The repo deliberately does not leave
    # Leo-specific package artefacts in /tmp or a global user cache.
    pip_tmp = LOCAL / "pip-tmp"
    pip_cache = LOCAL / "pip-cache"
    secure_dir(pip_tmp); secure_dir(pip_cache)
    pip_env = dict(os.environ, TMPDIR=str(pip_tmp), TEMP=str(pip_tmp), TMP=str(pip_tmp),
                   PIP_CACHE_DIR=str(pip_cache))
    r = subprocess.run(cmd, text=True, env=pip_env)
    if r.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        print("Runtime dependency installation failed. Check network/index access and retry; "
              "the existing venv was not removed.", file=sys.stderr)
        return r.returncode
    try:
        if VENV.exists():
            os.replace(VENV, old)
        os.replace(staging, VENV)
        shutil.rmtree(old, ignore_errors=True)
    except OSError as exc:
        if old.exists() and not VENV.exists():
            os.replace(old, VENV)
        shutil.rmtree(staging, ignore_errors=True)
        print(f"Runtime atomic swap failed; prior runtime restored: {exc}", file=sys.stderr)
        return 1
    current = version_of(str(VENV_PYTHON))
    atomic_json(STATE, {
        "requirementsSha": requirements_sha(),
        "venvPython": str(VENV_PYTHON),
        "pythonVersion": list(current or ()),
        "bootstrapPython": bootstrap,
        "bootstrapVersion": list(bootstrap_version or ()),
    })
    print(json.dumps(status(), indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="leos-runtime.py")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup", help="create or refresh local/.venv")
    p.add_argument("--python", help="explicit CPython 3.9+ bootstrap interpreter")
    p.add_argument("--refresh", action="store_true", help="document an intentional dependency refresh")
    sub.add_parser("status", help="print runtime health as JSON")
    args = ap.parse_args()
    if args.command == "setup":
        return setup(args)
    report = status()
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
