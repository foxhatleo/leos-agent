#!/usr/bin/env python3
"""state: machine-local JSON state for leos-agent skills and agents.

State lives at $LEOS_AGENT_PATH/local/<name>.json (LEOS_AGENT_PATH is an
optional override; unset, it defaults to ~/.leos-agent). local/ is gitignored:
state never syncs between machines. Top-level keys are "owner/repo" (or an
absolute project path when there is no GitHub repo) so data stays separate per
repo/project.

  state.py get   <name> [<repo-key>]        print the repo's subtree, or the
                                            whole file with no key ({} if absent)
  state.py merge <name> <repo-key> <json>   deep-merge <json> into the subtree
  state.py path  <name>                     print the backing file's path

merge semantics: dicts merge recursively, lists union in order (deduped,
so merging {"reviewed": [13]} twice never double-adds), scalars overwrite.
Writes are atomic (tempfile + os.replace). Exit codes: 0 ok, non-zero on error.
"""
import json
import os
import sys
import tempfile


def state_file(name):
    root = os.environ.get("LEOS_AGENT_PATH") or os.path.expanduser("~/.leos-agent")
    if not os.path.isdir(root):
        sys.exit(f"state: {root} does not exist — clone leos-agent there or set LEOS_AGENT_PATH")
    return os.path.join(root, "local", f"{name}.json")


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        sys.exit(f"state: {path} is corrupt ({e}) — fix or delete it")


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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
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
        data = load(path)
        data[argv[2]] = deep_merge(data.get(argv[2], {}), patch)
        atomic_write(path, data)
        print(json.dumps(data[argv[2]], indent=1, sort_keys=True))
    else:
        sys.exit(__doc__.strip())


if __name__ == "__main__":
    main(sys.argv[1:])
