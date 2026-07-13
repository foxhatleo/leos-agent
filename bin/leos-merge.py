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
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
try:  # Python 3.11+; the local runtime installs tomli for Python 3.9–3.10.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on the Python 3.9 CI lane
    import tomli as tomllib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LOCAL = os.environ.get("LEOS_LOCAL", os.path.join(REPO_ROOT, "local"))  # override for tests
STATE = os.path.join(LOCAL, "merge-state.json")
HOME = os.path.realpath(os.path.expanduser("~"))


def _expand_home_token(path):
    """Expand the one portable host-home token used in committed link maps."""
    return path.replace("{{CODEX_HOME}}", os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex")))


def sha_text(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def expand(dest):
    """Expand a dest and REFUSE anything outside $HOME (all host homes live under it).
    Resolve parents but not the final component so a legacy fragment symlink can be classified and
    replaced instead of following it into the clone."""
    raw = os.path.abspath(os.path.expanduser(_expand_home_token(dest)))
    path = os.path.join(os.path.realpath(os.path.dirname(raw)), os.path.basename(raw))
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


def _claimed_values(fragment, pre_merge, prev_owned):
    """Subset of the fragment Leo may honestly record as owned: values that were already present
    and identical in the destination BEFORE this merge — and were never previously Leo-owned —
    belong to the user, so removal must not retire them and a later fragment change must conflict
    rather than update-owned them. Anything in prev_owned stays claimed so a re-merge does not
    misclassify Leo's own previously-applied values."""
    out = {}
    prev_owned = prev_owned if isinstance(prev_owned, dict) else {}
    for k, v in fragment.items():
        pv = prev_owned.get(k)
        cv = pre_merge.get(k) if isinstance(pre_merge, dict) else None
        if isinstance(v, dict) and isinstance(cv, dict):
            sub = _claimed_values(v, cv, pv if isinstance(pv, dict) else {})
            if sub:
                out[k] = sub
        elif isinstance(v, list) and isinstance(cv, list):
            prev_list = pv if isinstance(pv, list) else []
            claimed = [x for x in v if x not in cv or x in prev_list]
            if claimed:
                out[k] = claimed
        elif cv == v and pv != v:
            continue
        else:
            out[k] = _copy(v)
    return out


def _expand_tokens(obj, mapping):
    """Recursively replace token substrings in every string value (e.g. {{CLONE_ROOT}} -> the clone
    path). Committed fragments carry the token so a machine-local absolute path is never committed;
    it is expanded here, at merge time."""
    if isinstance(obj, dict):
        return {k: _expand_tokens(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_tokens(v, mapping) for v in obj]
    if isinstance(obj, str):
        for tok, val in mapping.items():
            obj = obj.replace(tok, val)
        return obj
    return obj


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


def _toml_inline_comment(line):
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
        elif char in ("'", '"'):
            quote = None if quote == char else (char if quote is None else quote)
        elif char == "#" and quote is None:
            return line[index:].rstrip("\n")
    return ""


def _value_at(data, path):
    node = data
    for key in path:
        node = node[key]
    return node


def patch_toml_text(original, merged, actions):
    """Apply owned leaf actions to TOML text, preserving unrelated comments/order when possible."""
    if not original.strip():
        return dump_toml(merged)
    expanded = []
    for action in actions:
        if action["op"] != "retire":
            try:
                value = _value_at(merged, action["path"])
            except (KeyError, TypeError):
                value = None
            if isinstance(value, dict):
                def add_leaves(node, prefix):
                    for key, child in node.items():
                        if isinstance(child, dict):
                            add_leaves(child, prefix + [key])
                        else:
                            expanded.append({"op": "set", "path": prefix + [key]})
                add_leaves(value, action["path"])
                continue
        expanded.append(action)
    actions = expanded
    lines = original.splitlines(keepends=True)
    for action in actions:
        path = action["path"]
        if action["op"] == "retire":
            owned_header = "[" + ".".join(_toml_key(key) for key in path) + "]"
            header_index = next((i for i, line in enumerate(lines) if line.strip() == owned_header), None)
            if header_index is not None:
                section_end = next((i for i in range(header_index + 1, len(lines))
                                    if lines[i].lstrip().startswith("[")), len(lines))
                del lines[header_index:section_end]
                continue
        section = ".".join(_toml_key(key) for key in path[:-1])
        header = f"[{section}]" if section else None
        start = 0
        if header:
            header_index = next((i for i, line in enumerate(lines) if line.strip() == header), None)
            if header_index is None:
                if action["op"] == "retire":
                    continue
                if lines and lines[-1].strip():
                    lines.append("\n")
                lines.extend([header + "\n", f"{_toml_key(path[-1])} = {_toml_value(_value_at(merged, path))}\n"])
                continue
            start = header_index + 1
        end = next((i for i in range(start, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        key_text = _toml_key(path[-1])
        found = None
        for i in range(start, end):
            stripped = lines[i].lstrip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.split("=", 1)[0].strip() in (key_text, str(path[-1])):
                found = i
                break
        if action["op"] == "retire":
            if found is not None:
                lines.pop(found)
            continue
        rendered = _toml_value(_value_at(merged, path))
        if found is not None:
            indent = lines[found][:len(lines[found]) - len(lines[found].lstrip())]
            comment = _toml_inline_comment(lines[found].split("=", 1)[1])
            suffix = f"  {comment}" if comment else ""
            lines[found] = f"{indent}{key_text} = {rendered}{suffix}\n"
        else:
            lines.insert(end, f"{key_text} = {rendered}\n")
    candidate = "".join(lines)
    try:
        if tomllib.loads(candidate) == merged:
            return candidate
    except Exception:
        pass
    return dump_toml(merged)


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
                    # Conflict only when the user removed an owned element the fragment STILL wants
                    # (o not in cur[k] and o in v). An element retired by BOTH sides (not in cur[k]
                    # and not in v) is an agreed retire — a no-op, not a conflict.
                    edited = [o for o in own_list if o not in cur[k] and o in v]
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
            elif isinstance(ov, dict) and isinstance(cur[k], dict):
                # A user may have added their own keys inside a Leo-owned dict; retire Leo's
                # leaves individually and collapse to one dict-level retire only when every
                # remaining key retired cleanly (the dict was fully Leo's).
                conflict_mark, action_mark = len(conflicts), len(actions)
                retire(frag.get(k) if isinstance(frag.get(k), dict) else {}, cur[k], ov, p)
                retired = {tuple(a["path"]) for a in actions[action_mark:] if a["op"] == "retire"}
                if len(conflicts) == conflict_mark \
                        and all(tuple(p + [ck]) in retired for ck in cur[k]):
                    del actions[action_mark:]
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
    _atomic_write(STATE, json.dumps(state, indent=2, sort_keys=True) + "\n", mode=0o600)


def _secure_mkdir(path):
    os.makedirs(path, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _atomic_write(path, text, mode=0o600):
    """Stage under local/ then atomically replace.  Refuse cross-device writes rather than
    quietly putting Leo's temporary files in a system temp directory."""
    staging = os.path.join(LOCAL, "staging")
    _secure_mkdir(staging)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if os.stat(staging).st_dev != os.stat(parent).st_dev:
        raise OSError("local staging and destination are on different filesystems; refusing non-atomic write")
    fd, tmp = tempfile.mkstemp(prefix="merge-", dir=staging)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _merge_lock():
    """Serialize state+destination updates across concurrent setup/doctor sessions."""
    _secure_mkdir(LOCAL)
    lock_path = os.path.join(LOCAL, "merge.lock")
    with open(lock_path, "a+") as lock:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        try:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # macOS and mainstream Linux provide fcntl.  If unavailable, preserve correctness of
            # individual atomic writes but do not pretend we have inter-process serialization.
            pass
        try:
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


def snapshot_dest(dest, dest_key):
    if not os.path.exists(dest):
        return None
    bdir = os.path.join(LOCAL, "backups", time.strftime("%Y%m%d-%H%M%S"))
    _secure_mkdir(bdir)
    base = dest_key.replace("{{CODEX_HOME}}/", "codex-home__").replace("~/", "").replace("/", "__")
    target = os.path.join(bdir, base)
    n = 1
    while os.path.lexists(target):
        target = os.path.join(bdir, f"{base}.{n}")
        n += 1
    shutil.copy2(dest, target)
    return target


def _package_manager_extras(package_manager):
    """Render the selected Claude package-manager rules from the canonical policy data."""
    policy = load_json(os.path.join(REPO_ROOT, "core", "policy", "policy-data.json"), {})
    commands = policy.get("commandAllow", {}).get(package_manager, [])
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise ValueError(f"invalid commandAllow.{package_manager} policy data")
    return ({"permissions": {"allow": [f"Bash({cmd}:*)" for cmd in commands]}},
            {"packageManager": package_manager,
             "packageManagerSha": sha_text(json.dumps(commands, sort_keys=True))})


def _recorded_package_manager():
    """Retain legacy package-manager metadata without reintroducing lifecycle approvals."""
    try:
        state = load_state()
        entry = state.get("merges", {}).get("~/.claude/settings.json", {})
        pm = entry.get("packageManager")
        return pm if pm in ("pnpm", "yarn", "npm") else None
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return None


def do_merge(dest_key, fragment_path, strategy, force, extra_values=None, extra_meta=None):
    strip = fragment_path
    try:
        if strategy == "merge-toml":
            fragment = load_toml(strip, None)
        else:
            fragment = load_json(strip, None)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"applied": False, "dest": dest_key, "reason": f"cannot read fragment: {e}"}
    if not isinstance(fragment, dict):
        return {"applied": False, "dest": dest_key, "reason": f"cannot read fragment: {fragment_path}"}
    fragment = {k: v for k, v in fragment.items() if not str(k).startswith("$")}
    # Hash the TEMPLATE (tokens unexpanded) so drift detection stays machine-independent and matches
    # leos-doctor.frag_sha; then expand machine-local tokens for the actual merge + stored values.
    template_sha = sha_text(json.dumps(fragment, sort_keys=True))
    fragment = _expand_tokens(fragment, {"{{CLONE_ROOT}}": REPO_ROOT})
    extra_values = extra_values or {}
    extra_meta = extra_meta or {}
    fragment = _deep_union(_copy(fragment), extra_values)

    dest = expand(dest_key)
    legacy_fragment_link = os.path.islink(dest) and os.path.realpath(dest) == os.path.realpath(fragment_path)
    if os.path.islink(dest) and not legacy_fragment_link:
        return {"applied": False, "dest": dest_key,
                "reason": "foreign destination symlink refused; merge its target only after explicit approval"}
    try:
        current = load_toml(dest, {}) if strategy == "merge-toml" else load_json(dest, {})
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"applied": False, "dest": dest_key, "reason": f"cannot parse destination: {e}"}
    # apply_actions mutates `current` in place; keep the pre-merge shape so the ownership
    # snapshot can exclude values the user already had before Leo ever touched this file.
    pre_merge = _copy(current)
    try:
        state = load_state()
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"applied": False, "dest": dest_key, "reason": f"cannot parse merge state: {e}"}
    if not isinstance(state, dict) or not isinstance(state.get("merges"), dict):
        return {"applied": False, "dest": dest_key, "reason": "invalid merge state schema"}
    # A pre-CODEX_HOME state record used ~/.codex/config.toml.  Read it once and migrate it to the
    # portable key when the merge succeeds; no host file is rewritten merely to migrate metadata.
    legacy_key = dest_key.replace("{{CODEX_HOME}}", "~/.codex")
    entry = state["merges"].get(dest_key, state["merges"].get(legacy_key, {}))
    owned = _deep_union(_copy(entry.get("values", {})), entry.get("extraValues", {}))
    actions, conflicts = merge_preview(fragment, current, owned,
                                       retire_snapshot=entry.get("values", {}))
    if conflicts and not force:
        return {"applied": False, "dest": dest_key, "conflicts": conflicts}
    if not actions and not conflicts and not legacy_fragment_link:
        # No-op: keep the drift snapshot honest without rewriting the dest.
        state["merges"][dest_key] = {"values": _claimed_values(fragment, pre_merge, entry.get("values", {})),
                                      "extraValues": extra_values,
                                      "fragmentSha": template_sha, "strategy": strategy, **extra_meta}
        if legacy_key != dest_key:
            state["merges"].pop(legacy_key, None)
        save_state(state)
        return {"applied": True, "dest": dest_key, "actions": [], "note": "already current"}
    merged = apply_actions(current, actions)
    if conflicts:  # forced: fragment wins; retire sentinel deletes the key
        merged = apply_actions(merged, [
            {"op": "retire", "path": c["path"]} if c["ours"] == "<retired in new version>"
            else {"op": "set", "path": c["path"], "value": c["ours"]} for c in conflicts])
    try:
        if strategy == "merge-toml":
            try:
                with open(dest, encoding="utf-8") as f:
                    original_text = f.read()
            except FileNotFoundError:
                original_text = ""
            text = patch_toml_text(original_text, merged, actions)
        else:
            text = json.dumps(merged, indent=2)
        backup = snapshot_dest(dest, dest_key)
        old_mode = stat.S_IMODE(os.stat(dest).st_mode) if os.path.exists(dest) else 0o600
        _atomic_write(dest, text, mode=old_mode)
    except (OSError, TypeError, ValueError) as e:
        return {"applied": False, "dest": dest_key, "reason": f"write failed: {e}"}
    state["merges"][dest_key] = {"values": _claimed_values(fragment, pre_merge, entry.get("values", {})),
                                 "extraValues": extra_values,
                                 "fragmentSha": template_sha, "strategy": strategy, **extra_meta,
                                 "backup": backup}
    if legacy_key != dest_key:
        state["merges"].pop(legacy_key, None)
    save_state(state)
    return {"applied": True, "dest": dest_key, "actions": actions, "backup": backup,
            "migratedLegacyFragmentLink": legacy_fragment_link}


def do_remove(dest_key, strategy):
    """Remove only values still equal to Leo's ownership snapshot; never restore a whole backup."""
    dest = expand(dest_key)
    try:
        state = load_state()
        legacy_key = dest_key.replace("{{CODEX_HOME}}", "~/.codex")
        entry = state.get("merges", {}).get(dest_key, state.get("merges", {}).get(legacy_key))
        if not isinstance(entry, dict):
            return {"applied": True, "dest": dest_key, "actions": [], "note": "not owned"}
        if os.path.islink(dest):
            # Mirrors do_merge's refusal: writing through would replace the user's symlink with
            # a regular file. Post-merge dests are regular files, so a symlink here is foreign.
            return {"applied": False, "dest": dest_key,
                    "reason": "foreign destination symlink refused; remove Leo's values from its target manually"}
        current = load_toml(dest, {}) if strategy == "merge-toml" else load_json(dest, {})
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"applied": False, "dest": dest_key, "reason": f"cannot prepare removal: {e}"}
    owned = _deep_union(_copy(entry.get("values", {})), entry.get("extraValues", {}))
    actions, conflicts = merge_preview({}, current, owned)
    if conflicts:
        return {"applied": False, "dest": dest_key, "conflicts": conflicts,
                "reason": "owned values were modified; preserving destination"}
    merged = apply_actions(current, actions)
    try:
        if actions:
            if strategy == "merge-toml":
                with open(dest, encoding="utf-8") as f:
                    text = patch_toml_text(f.read(), merged, actions)
            else:
                text = json.dumps(merged, indent=2)
            snapshot_dest(dest, dest_key)
            old_mode = stat.S_IMODE(os.stat(dest).st_mode) if os.path.exists(dest) else 0o600
            _atomic_write(dest, text, mode=old_mode)
        state["merges"].pop(dest_key, None)
        state["merges"].pop(legacy_key, None)
        save_state(state)
    except (OSError, TypeError, ValueError) as e:
        return {"applied": False, "dest": dest_key, "reason": f"removal write failed: {e}"}
    return {"applied": True, "dest": dest_key, "actions": actions}


def main():
    ap = argparse.ArgumentParser(prog="leos-merge.py")
    ap.add_argument("--tool", choices=["claude", "codex", "opencode", "cursor"])
    ap.add_argument("--dest")
    ap.add_argument("--fragment")
    ap.add_argument("--strategy", choices=["merge-json", "merge-toml"])
    ap.add_argument("--package-manager", choices=["pnpm", "yarn", "npm"],
                    help="legacy Claude metadata compatibility; package scripts are not pre-approved")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--remove", action="store_true",
                    help="with --tool (or --dest/--strategy), remove only Leo-owned values")
    args = ap.parse_args()

    results = []
    with _merge_lock():
        if args.tool:
            linkmap = load_json(os.path.join(REPO_ROOT, "tools", args.tool, "linkmap.json"), {})
            extras = {}
            extra_meta = {}
            selected_pm = args.package_manager
            if args.tool == "claude" and selected_pm is None:
                selected_pm = _recorded_package_manager()
            if selected_pm:
                if args.tool != "claude":
                    ap.error("--package-manager is currently a Claude settings surface only")
                try:
                    extras, extra_meta = _package_manager_extras(selected_pm)
                except ValueError as e:
                    ap.error(str(e))
            for m in linkmap.get("merges", []):
                if args.remove:
                    results.append(do_remove(m["dest"], m["strategy"]))
                else:
                    frag = os.path.join(REPO_ROOT, m["fragment"])
                    results.append(do_merge(m["dest"], frag, m["strategy"], args.force, extras, extra_meta))
        elif args.dest and args.strategy and args.remove:
            results.append(do_remove(args.dest, args.strategy))
        elif args.dest and args.fragment and args.strategy:
            results.append(do_merge(args.dest, args.fragment, args.strategy, args.force))
        else:
            ap.error("give --tool, or all of --dest --fragment --strategy")
    print(json.dumps(results, indent=2))
    return 1 if any(not r.get("applied") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
