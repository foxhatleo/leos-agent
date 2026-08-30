#!/usr/bin/env python3
"""state: machine-local JSON state for leos-agent skills and agents.

CODE ships inside the plugin (possibly a versioned cache that a plugin update
can wipe or relocate) — this file must never derive the data root from its own
__file__ location. DATA always lives under
${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}, independent of where this
script itself happens to run from, so a plugin update can never lose state.

State lives at $LEOS_AGENT_LOCAL_PATH/<name>.json (LEOS_AGENT_LOCAL_PATH is an
optional override; unset, it defaults to ~/.leos-agent-local). The base is a
dedicated data directory rather than a repo root, so there is no nested
local/ segment inside it: state never syncs between machines. Top-level
keys are "owner/repo" (or an
absolute project path when there is no GitHub repo) so data stays separate per
repo/project.

  state.py get   <name> [<repo-key>]        print the repo's subtree, or the
                                            whole file with no key ({} if absent)
  state.py merge <name> <repo-key> <json>   deep-merge <json> into the subtree
  state.py path  <name>                     print the backing file's path

merge semantics: dicts merge recursively, lists union in order (deduped,
so merging {"reviewed": [13]} twice never double-adds), scalars overwrite.
merge calls are serialized (flock on a sibling <name>.json.lock) and each
write is atomic (tempfile + os.replace), so concurrent merges from parallel
agents never lose an update; get is lock-free. Exit codes: 0 ok, non-zero on
error.
"""
import contextlib
import fcntl
import json
import os
import sys
import tempfile


def _data_root():
    return os.environ.get("LEOS_AGENT_LOCAL_PATH") or os.path.join(os.path.expanduser("~"), ".leos-agent-local")


def state_file(name):
    if "/" in name or "\\" in name or ".." in name or os.path.isabs(name):
        sys.exit(f"state: {name!r} is not a valid state name (no slashes, no .., not absolute)")
    root = _data_root()
    # 0700: the root holds handoffs and per-repo state — project context that
    # is nobody else's business on a shared machine. Applies on creation only;
    # an existing directory keeps whatever Leo set on it.
    os.makedirs(root, mode=0o700, exist_ok=True)
    return os.path.join(root, f"{name}.json")


@contextlib.contextmanager
def _locked(path):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        sys.exit(f"state: {path} is corrupt ({e}) — fix or delete it")
    if not isinstance(data, dict):
        sys.exit(f"state: {path} is corrupt (top level is {type(data).__name__}, expected object) — fix or delete it")
    return data


def deep_merge(base, patch):
    if isinstance(patch, dict):
        merged = dict(base) if isinstance(base, dict) else {}
        for key, value in patch.items():
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    if isinstance(patch, list):
        merged = list(base) if isinstance(base, list) else []
        for v in patch:
            if v not in merged:
                merged.append(v)
        return merged
    return patch


def atomic_write(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main(argv):
    if len(argv) >= 2 and argv[0] == "path":
        print(state_file(argv[1]))
    elif len(argv) >= 2 and argv[0] == "get":
        data = load(state_file(argv[1]))
        if len(argv) >= 3:
            data = data.get(argv[2], {})
        print(json.dumps(data, indent=1, sort_keys=True))
    elif len(argv) == 4 and argv[0] == "merge":
        try:
            patch = json.loads(argv[3])
        except json.JSONDecodeError as e:
            sys.exit(f"state: patch is not valid JSON ({e})")
        path = state_file(argv[1])
        with _locked(path):
            data = load(path)
            data[argv[2]] = deep_merge(data.get(argv[2], {}), patch)
            atomic_write(path, data)
        print(json.dumps(data[argv[2]], indent=1, sort_keys=True))
    else:
        sys.exit(__doc__.strip())


if __name__ == "__main__":
    main(sys.argv[1:])
