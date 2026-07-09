#!/usr/bin/env python3
"""leos-merge — merge tool-config fragments into the files a host rewrites.

Symlinks deliver everything EXCEPT the handful of files a host tool writes itself
(~/.claude/settings.json, ~/.codex/config.toml, ~/.config/opencode/opencode.json,
~/.cursor/cli-config.json). Those are merged, not linked. This tool merges a committed
fragment into such a dest with union/retire semantics, backs the dest up first, refuses on an
unowned conflict, and records the merge in local/merge-state.json so leos-doctor can detect when
a committed fragment later drifts from what was merged (the only thing `git pull` cannot
auto-apply).

Usage:
  leos-merge.py --tool {claude|codex|opencode|cursor}      # merge every fragment in that linkmap
  leos-merge.py --dest PATH --fragment F --strategy {merge-json|merge-toml} [--force]

Stdlib only. Fails loudly. Writes only to the dest, its backup, and local/merge-state.json.
Ported from leos-codex tools/apply.py (merge engine + TOML round-trip gate), minus the
copy/ownership-sha machinery that symlinks make obsolete.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import tomllib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LOCAL = os.environ.get("LEOS_LOCAL", os.path.join(REPO_ROOT, "local"))  # override for tests
STATE = os.path.join(LOCAL, "merge-state.json")
HOME = os.path.realpath(os.path.expanduser("~"))


def sha_text(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def expand(dest):
    """Expand a dest and REFUSE anything outside $HOME (all host homes live under it).
    realpath kills ../ traversal and symlink tricks."""
    path = os.path.realpath(os.path.expanduser(dest))
    if not (path == HOME or path.startswith(HOME + os.sep)):
        raise SystemExit(f"refusing dest outside HOME: {dest}")
    return path


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def load_toml(path, default):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return default


def _copy(obj):
    return json.loads(json.dumps(obj))


def _deep_union(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_union(base[k], v)
        elif isinstance(v, list) and isinstance(base.get(k), list):
            base[k] = base[k] + [x for x in v if x not in base[k]]
        else:
            base[k] = v
    return base


# --- TOML serialization (round-trip gated) -----------------------------------

def _toml_key(key):
    key = str(key)
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"unsupported TOML value: {value!r}")


def dump_toml(data):
    lines = []

    def emit_table(table, prefix):
        for key in sorted(k for k, v in table.items() if not isinstance(v, dict)):
            lines.append(f"{_toml_key(key)} = {_toml_value(table[key])}")
        for key in sorted(k for k, v in table.items() if isinstance(v, dict)):
            child = prefix + [_toml_key(key)]
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"[{'.'.join(child)}]")
            emit_table(table[key], child)

    emit_table(data, [])
    text = "\n".join(lines).rstrip() + "\n"
    if tomllib.loads(text) != data:
        raise ValueError("TOML serialization round-trip mismatch — refusing to write")
    return text


# --- merge engine (from apply.py) --------------------------------------------

def merge_preview(fragment, current, owned_values, retire_missing=True, retire_snapshot=None):
    actions, conflicts = [], []

    def walk(frag, cur, own, path):
        for k, v in frag.items():
            p = path + [k]
            if k not in cur:
                actions.append({"op": "set", "path": p, "value": v})
            elif isinstance(v, list) and isinstance(cur[k], list):
                own_list = own.get(k) if isinstance(own.get(k), list) else []
                if own_list:
                    # Ownership-aware for ALL arrays (strings incl.): superseded owned elements
                    # are swapped out on shrink; machine-added elements are preserved. (Wider than
                    # apply.py, which only tracked dict-element arrays.)
                    lingering = [o for o in own_list if o in cur[k] and o not in v]
                    if all(x in cur[k] for x in v) and not lingering:
                        continue
                    edited = [o for o in own_list if o not in cur[k] and o not in v]
                    if edited:
                        conflicts.append({"path": p, "ours": v,
                                          "theirs": "owned element(s) modified/removed by user",
                                          "detail": edited})
                        continue
                    superseded = [o for o in own_list if o in cur[k] and o not in v]
                    new_elems = [x for x in v if x not in cur[k]]
                    if superseded or new_elems:
                        actions.append({"op": "replace-elements", "path": p,
                                        "remove": superseded, "add": new_elems})
                else:
                    missing = [x for x in v if x not in cur[k]]
                    if missing:
                        actions.append({"op": "append", "path": p, "value": missing})
            elif isinstance(v, dict) and isinstance(cur[k], dict):
                walk(v, cur[k], own.get(k, {}) if isinstance(own.get(k), dict) else {}, p)
            elif cur[k] == v:
                pass
            elif own.get(k) == cur[k]:
                actions.append({"op": "update-owned", "path": p, "value": v, "was": cur[k]})
            else:
                conflicts.append({"path": p, "ours": v, "theirs": cur[k]})

    def retire(frag, cur, own, path):
        for k, ov in own.items():
            p = path + [k]
            if k in frag:
                if isinstance(ov, dict) and isinstance(frag.get(k), dict) and isinstance(cur.get(k), dict):
                    retire(frag[k], cur[k], ov, p)
                continue
            if k not in cur:
                continue
            if cur[k] == ov:
                actions.append({"op": "retire", "path": p})
            elif isinstance(ov, list) and isinstance(cur[k], list):
                present = [o for o in ov if o in cur[k]]
                if present:
                    actions.append({"op": "replace-elements", "path": p,
                                    "remove": present, "add": []})
            else:
                conflicts.append({"path": p, "ours": "<retired in new version>",
                                  "theirs": cur[k]})

    # $-prefixed doc keys in fragments are annotations, not data — never merge them.
    fragment = {k: v for k, v in fragment.items() if not str(k).startswith("$")}
    walk(fragment, current, owned_values, [])
    if retire_missing:
        retire(fragment, current,
               retire_snapshot if retire_snapshot is not None else owned_values, [])
    return actions, conflicts


def apply_actions(current, actions):
    for a in actions:
        keys = a["path"]
        node = current
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        leaf = keys[-1]
        if a["op"] == "retire":
            node.pop(leaf, None)
        elif a["op"] == "append":
            node.setdefault(leaf, [])
            node[leaf] = node[leaf] + [x for x in a["value"] if x not in node[leaf]]
        elif a["op"] == "replace-elements":
            node.setdefault(leaf, [])
            node[leaf] = [x for x in node[leaf] if x not in a["remove"]] \
                + [x for x in a["add"] if x not in node[leaf]]
        else:
            node[leaf] = a["value"]
    return current


# --- state + backup ----------------------------------------------------------

def load_state():
    return load_json(STATE, {"merges": {}})


def save_state(state):
    os.makedirs(LOCAL, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def snapshot_dest(dest, dest_key):
    if not os.path.exists(dest):
        return None
    bdir = os.path.join(LOCAL, "backups", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bdir, exist_ok=True)
    target = os.path.join(bdir, dest_key.replace("~/", "").replace("/", "__"))
    shutil.copy2(dest, target)
    return target


def do_merge(dest_key, fragment_path, strategy, force):
    strip = fragment_path
    if strategy == "merge-toml":
        fragment = load_toml(strip, None)
    else:
        fragment = load_json(strip, None)
    if not isinstance(fragment, dict):
        return {"applied": False, "dest": dest_key, "reason": f"cannot read fragment: {fragment_path}"}
    fragment = {k: v for k, v in fragment.items() if not str(k).startswith("$")}

    dest = expand(dest_key)
    current = load_toml(dest, {}) if strategy == "merge-toml" else load_json(dest, {})
    state = load_state()
    entry = state["merges"].get(dest_key, {})
    owned = _deep_union(_copy(entry.get("values", {})), entry.get("extraValues", {}))
    actions, conflicts = merge_preview(fragment, current, owned,
                                       retire_snapshot=entry.get("values", {}))
    frag_text = json.dumps(fragment, sort_keys=True)
    if conflicts and not force:
        return {"applied": False, "dest": dest_key, "conflicts": conflicts}
    if not actions and not conflicts:
        # No-op: keep the drift snapshot honest without rewriting the dest.
        state["merges"][dest_key] = {"values": fragment, "extraValues": entry.get("extraValues", {}),
                                     "fragmentSha": sha_text(frag_text), "strategy": strategy}
        save_state(state)
        return {"applied": True, "dest": dest_key, "actions": [], "note": "already current"}
    merged = apply_actions(current, actions)
    if conflicts:  # forced: fragment wins; retire sentinel deletes the key
        merged = apply_actions(merged, [
            {"op": "retire", "path": c["path"]} if c["ours"] == "<retired in new version>"
            else {"op": "set", "path": c["path"], "value": c["ours"]} for c in conflicts])
    try:
        text = dump_toml(merged) if strategy == "merge-toml" else json.dumps(merged, indent=2)
        backup = snapshot_dest(dest, dest_key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as f:
            f.write(text)
    except (OSError, TypeError, ValueError) as e:
        return {"applied": False, "dest": dest_key, "reason": f"write failed: {e}"}
    state["merges"][dest_key] = {"values": fragment, "extraValues": entry.get("extraValues", {}),
                                 "fragmentSha": sha_text(frag_text), "strategy": strategy,
                                 "backup": backup}
    save_state(state)
    return {"applied": True, "dest": dest_key, "actions": actions, "backup": backup}


def main():
    ap = argparse.ArgumentParser(prog="leos-merge.py")
    ap.add_argument("--tool", choices=["claude", "codex", "opencode", "cursor"])
    ap.add_argument("--dest")
    ap.add_argument("--fragment")
    ap.add_argument("--strategy", choices=["merge-json", "merge-toml"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    results = []
    if args.tool:
        linkmap = load_json(os.path.join(REPO_ROOT, "tools", args.tool, "linkmap.json"), {})
        for m in linkmap.get("merges", []):
            frag = os.path.join(REPO_ROOT, m["fragment"])
            results.append(do_merge(m["dest"], frag, m["strategy"], args.force))
    elif args.dest and args.fragment and args.strategy:
        results.append(do_merge(args.dest, args.fragment, args.strategy, args.force))
    else:
        ap.error("give --tool, or all of --dest --fragment --strategy")
    print(json.dumps(results, indent=2))
    return 1 if any(not r.get("applied") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
