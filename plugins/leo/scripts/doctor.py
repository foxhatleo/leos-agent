#!/usr/bin/env python3
"""doctor: report how Leo's Agent is wired on this machine.

This script answers only what disk and environment can prove. It deliberately
does NOT claim the policy reached the model: a hook can be present, executable,
and correctly listed, and still have failed open this session. Only the running
agent can see its own context, so leo:doctor pairs this output with three
questions the model answers itself.

The breadcrumb logs are reported as history with unknown provenance, never as a
verdict about this session. They carry no timestamps and the test suite drives
the failure paths deliberately, so "the log has errors" proves nothing on its
own.

  doctor.py           human-readable report
  doctor.py --json    the same facts as JSON

Exit code is 0 unless the payload itself cannot be found.
"""
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.path.dirname(_HERE)
TIERS = ("fable", "opus", "sonnet", "haiku")


def _detect_harness():
    """Reuse hooks/session-start.py rather than re-deriving the rules.

    Its ordering carries two subtleties a second implementation gets wrong:
    Cursor must be tested first because it sets more than one variable, and the
    absence of CLAUDE_PLUGIN_ROOT is not a Codex signal. The filename is
    hyphenated and therefore not importable by name, so it loads by path — the
    same technique hooks/cursor-guard.py uses.
    """
    path = os.path.join(PAYLOAD, "hooks", "session-start.py")
    try:
        spec = importlib.util.spec_from_file_location("leo_session_start", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._detect_harness(), "hooks/session-start.py"
    except Exception:
        # Hermes and OpenCode never run that script and export no plugin-root
        # variable, so absence of every marker is itself the signal.
        if os.environ.get("CURSOR_PLUGIN_ROOT") or os.environ.get("CURSOR_VERSION"):
            return "cursor", "env"
        if os.environ.get("PLUGIN_ROOT"):
            return "codex", "env"
        if os.environ.get("CLAUDE_PLUGIN_ROOT"):
            return "claude", "env"
        return "unknown", "env"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _local_root():
    return os.environ.get("LEOS_AGENT_LOCAL_PATH") or os.path.join(
        os.path.expanduser("~"), ".leos-agent-local"
    )


def _memory_report():
    try:
        sys.path.insert(0, _HERE)
        import memory

        root = memory.memory_root()
        if not os.path.isdir(root):
            return {"store": root, "facts": 0, "present": False, "targets": []}
        index = memory._load_index() or {"facts": []}
        targets = [
            {"harness": h, "path": f, "present": os.path.exists(f),
             "projected": os.path.exists(f) and memory.BEGIN in _slurp(f)}
            for h, gate, f, _ in memory.projection_targets()
            if os.path.isdir(gate)
        ]
        return {"store": root, "facts": len(index["facts"]), "present": True,
                "targets": targets}
    except Exception as exc:
        return {"store": _local_root(), "error": f"{type(exc).__name__}: {exc}"}


def _slurp(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _breadcrumbs():
    out = {}
    for name in ("session-start.log", "hermes-policy.log", "opencode-guard.log"):
        path = os.path.join(_local_root(), name)
        if not os.path.exists(path):
            continue
        lines = [l for l in _slurp(path).splitlines() if l.strip()]
        if lines:
            out[name] = {"entries": len(lines), "newest": lines[-1][:120]}
    return out


def _skills():
    shipped = {}
    for root in ("skills", "skills-claude"):
        directory = os.path.join(PAYLOAD, root)
        shipped[root] = sorted(
            name for name in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, name, "SKILL.md"))
        ) if os.path.isdir(directory) else []
    return shipped


def collect():
    harness, source = _detect_harness()
    manifest = _read_json(os.path.join(PAYLOAD, ".claude-plugin", "plugin.json")) or {}
    models = _read_json(os.path.join(PAYLOAD, "config", "models.json")) or {}
    config = (models.get("harnesses") or {}).get(harness) or {}
    hook = os.path.join(PAYLOAD, "hooks", "session-start.py")
    local = _local_root()
    skills = _skills()
    claude_only = set((models.get("skills") or {}).get("claudeOnly") or ())
    excluded = set(((models.get("skills") or {}).get("exclude") or {}).get(harness) or ())

    registered = [n for n in skills["skills"] if n not in excluded]
    if harness == "claude":
        registered += skills["skills-claude"]

    return {
        "harness": {"value": harness, "source": source},
        "payload": {"path": PAYLOAD, "version": manifest.get("version")},
        "bootstrap": {
            "hook": hook,
            "present": os.path.isfile(hook),
            "executable": os.access(hook, os.X_OK),
        },
        "tiers": {
            tier: {"model": (config.get(tier) or {}).get("model"),
                   "effort": (config.get(tier) or {}).get("effort")}
            for tier in TIERS
        },
        "local_state": {
            "path": local,
            "present": os.path.isdir(local),
            "writable": os.access(local, os.W_OK) if os.path.isdir(local) else None,
        },
        "memory": _memory_report(),
        "skills": {
            "shipped_portable": skills["skills"],
            "shipped_claude_only": skills["skills-claude"],
            "expected_here": sorted(registered),
            "excluded_here": sorted(excluded | (set() if harness == "claude" else claude_only)),
        },
        "breadcrumbs": _breadcrumbs(),
    }


def _render(data):
    lines = ["leo doctor", ""]

    def row(label, value, source):
        # A long path must not shove the source column off the line: overflow
        # drops the source onto its own indented continuation instead.
        value = str(value)
        if len(value) > 44:
            lines.append(f"  {label:<16}{value}")
            lines.append(f"  {'':<16}{'':<44}{source}")
        else:
            lines.append(f"  {label:<16}{value:<44}{source}")

    harness = data["harness"]
    row("harness", harness["value"], f"detected via {harness['source']}")
    row("payload", f"{data['payload']['path']}", "disk")
    row("version", data["payload"]["version"] or "unknown", "disk")

    boot = data["bootstrap"]
    state = "present" if boot["present"] else "MISSING"
    if boot["present"] and not boot["executable"]:
        state += ", not executable"
    row("bootstrap", state, "disk")

    tiers = " · ".join(
        f"{t.capitalize()} {v['model']}" + (f"/{v['effort']}" if v["effort"] else "")
        for t, v in data["tiers"].items() if v["model"]
    )
    row("tiers", tiers or "no mapping for this harness", "config/models.json")

    local = data["local_state"]
    row("local state", local["path"],
        "disk: " + ("writable" if local["writable"] else
                    "present" if local["present"] else "not created yet"))

    mem = data["memory"]
    if mem.get("error"):
        row("memory", mem["error"], "disk")
    elif not mem["present"]:
        row("memory", "no store yet", "disk")
    else:
        row("memory", f"{mem['facts']} facts", "disk")
        for target in mem["targets"]:
            mark = "projected" if target["projected"] else "not projected"
            row("", f"  {target['harness']}: {mark}", target["path"])

    skills = data["skills"]
    row("skills shipped",
        f"{len(skills['shipped_portable'])} portable · "
        f"{len(skills['shipped_claude_only'])} claude-only", "disk")
    row("expected here", f"{len(skills['expected_here'])} skills", "disk + config")
    if skills["excluded_here"]:
        row("not available", ", ".join(skills["excluded_here"]), "config/models.json")

    if data["breadcrumbs"]:
        lines.append("")
        lines.append("  breadcrumbs (history, provenance unknown — not this session):")
        for name, info in data["breadcrumbs"].items():
            lines.append(f"    {name}: {info['entries']} entries, newest: {info['newest']}")

    lines += [
        "",
        "  This script proves what shipped to disk. It cannot prove the policy",
        "  reached the model — answer the three context questions in leo:doctor.",
    ]
    return "\n".join(lines)


def main(argv):
    data = collect()
    if "--json" in argv:
        print(json.dumps(data, indent=1, sort_keys=True))
    else:
        print(_render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
