#!/usr/bin/env python3
"""doctor: report how Leo's Agent is wired on this machine.

This script answers only what disk and environment can prove. It deliberately
does NOT claim the policy reached the model: a hook can be present, executable,
and correctly listed, and still have failed open this session. Only the running
agent can see its own context, so leo:doctor pairs this output with three
questions the model answers itself.

Most breadcrumb logs are reported as history with unknown provenance, never as
a verdict about this session. Some legacy entries carry no timestamps and the
test suite drives failure paths deliberately, so "the log has errors" usually
proves nothing on its own. `opencode-skills.log` is the capability exception:
its presence records a namespaced-skill registration failure and is reported
as degraded until it is intentionally cleared after repair.

  doctor.py                    human-readable report
  doctor.py --json             the same facts as JSON
  doctor.py --harness <name>   state the harness instead of detecting it

Exit code is nonzero when required payload/bootstrap artifacts are missing or
invalid, or the running Python is unsupported. Degraded historical warnings
remain report-only.
"""
import importlib.util
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.path.dirname(_HERE)
TIERS = ("fable", "opus", "sonnet", "haiku")
MIN_PYTHON = (3, 9)
EXPECTED_HARNESSES = {"claude", "codex", "cursor", "hermes", "opencode"}
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\."
    r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


HARNESS_ENV = ("CURSOR_PLUGIN_ROOT", "CURSOR_VERSION", "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")


def _known_harnesses():
    models = _read_json(os.path.join(PAYLOAD, "config", "models.json")) or {}
    harnesses = models.get("harnesses") if isinstance(models, dict) else None
    return set(harnesses) if isinstance(harnesses, dict) else set()


def _detect_harness(argv=()):
    """Three signals, most explicit first; never a guess.

    The env-var rules still live in hooks/session-start.py and are reused
    rather than re-derived, because their ordering carries two subtleties a
    second implementation gets wrong: Cursor must be tested first because it
    sets more than one variable, and the absence of CLAUDE_PLUGIN_ROOT is not
    a Codex signal. The filename is hyphenated and therefore not importable by
    name, so it loads by path — the same technique hooks/cursor-guard.py uses.

    What is new is that the delegation is *gated*. That function's final branch
    returns "claude" as a default, which is right for a hook that only ever
    runs on the three hook harnesses. doctor ships to five. Hermes and OpenCode
    run no hook and export no plugin-root variable, so doctor inherited the
    default and reported `claude` on both — printing Claude's tier table and
    listing four Claude-only skills as available on harnesses that have none of
    them. Absence of every marker means unknown, and unknown is reported.
    """
    known = _known_harnesses()

    # 1. Stated outright. leo:doctor tells the agent to pass the harness it can
    #    read off its own mapping heading. Validated against the config, so a
    #    typo degrades to detection rather than inventing a harness.
    argv = list(argv)
    for index, arg in enumerate(argv):
        name = None
        if arg == "--harness" and index + 1 < len(argv):
            name = argv[index + 1]
        elif arg.startswith("--harness="):
            name = arg.split("=", 1)[1]
        if name and name in known:
            return name, "--harness"

    # 2. A positive signal the two hookless harnesses set for themselves at
    #    registration (__init__.py and adapters/opencode/plugin.js).
    declared = os.environ.get("LEOS_AGENT_HARNESS")
    if declared and declared in known:
        return declared, "env (LEOS_AGENT_HARNESS)"

    # 3. The env-var rules, unchanged — but only when a marker actually exists.
    if not any(os.environ.get(var) for var in HARNESS_ENV):
        return "unknown", "no signal"

    path = os.path.join(PAYLOAD, "hooks", "session-start.py")
    try:
        spec = importlib.util.spec_from_file_location("leo_session_start", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._detect_harness(), "hooks/session-start.py"
    except Exception:
        if os.environ.get("CURSOR_PLUGIN_ROOT") or os.environ.get("CURSOR_VERSION"):
            return "cursor", "env"
        if os.environ.get("PLUGIN_ROOT"):
            return "codex", "env"
        return "claude", "env"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _manifest_valid(manifest):
    """Validate the required portable manifest fields doctor reports."""
    if not isinstance(manifest, dict):
        return False
    for key in ("name", "description", "homepage", "repository", "license"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            return False
    version = manifest.get("version")
    if not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None:
        return False
    author = manifest.get("author")
    if not isinstance(author, dict) or not isinstance(author.get("name"), str) \
            or not author["name"].strip():
        return False
    skills = manifest.get("skills")
    return (isinstance(skills, list) and bool(skills) and
            all(isinstance(item, str) and item.strip() for item in skills))


def _models_valid(models):
    """Apply structural checks and the canonical renderer validation.

    The renderer owns cross-field coherence, while these checks reject
    skeletal documents that happen not to exercise one of its relationships.
    Doctor must never call either shape healthy merely because it contains a
    ``harnesses`` object.
    """
    if not isinstance(models, dict) or models.get("schemaVersion") != 4:
        return False
    for key in ("roles", "harnesses", "skills", "mcp", "memoryTarget", "visual"):
        if not isinstance(models.get(key), dict) or not models[key]:
            return False
    capabilities = models.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        return False
    harnesses = models["harnesses"]
    if set(harnesses) != EXPECTED_HARNESSES:
        return False
    for rows in harnesses.values():
        if not isinstance(rows, dict):
            return False
        for tier in TIERS:
            mapping = rows.get(tier)
            if (not isinstance(mapping, dict) or
                    not isinstance(mapping.get("model"), str) or
                    not mapping["model"].strip()):
                return False
            if "effort" in mapping and (not isinstance(mapping["effort"], str) or
                                         not mapping["effort"].strip()):
                return False
    for spec in models["roles"].values():
        if (not isinstance(spec, dict) or spec.get("tier") not in TIERS or
                spec.get("access") not in ("read-only", "write")):
            return False
    for row in capabilities:
        if (not isinstance(row, dict) or not isinstance(row.get("key"), str) or
                not isinstance(row.get("modes"), list) or not row["modes"] or
                not isinstance(row.get("values"), dict)):
            return False

    validator_path = os.path.join(PAYLOAD, "scripts", "render_adapters.py")
    try:
        spec = importlib.util.spec_from_file_location("leo_doctor_models_validator",
                                                      validator_path)
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        validator._validate(models)
    except Exception:
        return False
    return True


def _local_root():
    return os.environ.get("LEOS_AGENT_LOCAL_PATH") or os.path.join(
        os.path.expanduser("~"), ".leos-agent-local"
    )


def _memory_report():
    try:
        sys.path.insert(0, _HERE)
        import memory

        root = memory.memory_root()
        hermes_enabled = memory.hermes_enabled()
        hermes_target = os.path.join(memory.hermes_home(), "SOUL.md")
        hermes_projection = {
            "enabled": hermes_enabled,
            "path": hermes_target,
            "status": (
                "disabled" if not hermes_enabled else
                "projected" if (os.path.isfile(hermes_target) and
                                memory.BEGIN in _slurp(hermes_target)) else
                "not projected"
            ),
        }
        if not os.path.isdir(root):
            return {"store": root, "facts": 0, "present": False, "targets": [],
                    "hermes_projection": hermes_projection}
        index = memory._load_index() or {"facts": []}
        targets = [
            {"harness": h, "path": f, "present": os.path.exists(f),
             "projected": os.path.exists(f) and memory.BEGIN in _slurp(f)}
            for h, gate, f, _, _ in memory.projection_targets()
            if os.path.isdir(gate)
        ]
        return {"store": root, "facts": len(index["facts"]), "present": True,
                "targets": targets, "hermes_projection": hermes_projection}
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
    for name in ("session-start.log", "hermes-policy.log", "opencode-guard.log",
                 "opencode-skills.log"):
        path = os.path.join(_local_root(), name)
        if not os.path.exists(path):
            continue
        lines = [l for l in _slurp(path).splitlines() if l.strip()]
        if lines:
            out[name] = {"entries": len(lines), "newest": lines[-1][:120]}
    return out


def _contains(path, needle):
    return needle in _slurp(path)


def _hook_manifest_valid(data, harness):
    """Validate the session-start registration, not a stray filename mention."""
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return False
    event = "sessionStart" if harness == "cursor" else "SessionStart"
    groups = data["hooks"].get(event)
    if not isinstance(groups, list):
        return False
    hooks = groups if harness == "cursor" else [
        hook
        for group in groups if isinstance(group, dict)
        for hook in (group.get("hooks") or []) if isinstance(hook, dict)
    ]
    return any(
        isinstance(hook, dict)
        and isinstance(hook.get("command"), str)
        and "hooks/session-start.py" in hook["command"]
        for hook in hooks
    )


def _bootstrap(harness):
    """Describe the harness's actual policy delivery mechanism.

    Presence is intentionally a payload check, not a claim that the host ran
    it. Codex hook trust is visible only in its `/hooks` UI, and neither
    Hermes nor OpenCode exposes a host-side registration ledger to inspect.
    """
    hook = os.path.join(PAYLOAD, "hooks", "session-start.py")
    if harness in ("claude", "codex", "cursor"):
        manifest = os.path.join(
            PAYLOAD, "hooks", "hooks-cursor.json" if harness == "cursor" else "hooks.json"
        )
        manifest_data = _read_json(manifest)
        data = {"kind": "session hook", "hook": hook,
                "present": os.path.isfile(hook),
                "executable": os.access(hook, os.X_OK),
                "manifest": manifest,
                "manifest_valid": _hook_manifest_valid(manifest_data, harness),
                "required": ("present", "executable", "manifest_valid")}
        if harness == "codex":
            data["trust_instruction"] = (
                "Codex cannot be verified from disk; run /hooks and confirm this plugin is trusted."
            )
        return data
    if harness == "opencode":
        plugin = os.path.join(PAYLOAD, "adapters", "opencode", "plugin.js")
        return {"kind": "config.instructions", "plugin": plugin,
                "present": os.path.isfile(plugin),
                "instructions": _contains(plugin, "config.instructions"),
                "required": ("present", "instructions")}
    if harness == "hermes":
        entry = os.path.join(os.path.dirname(PAYLOAD), "..", "__init__.py")
        entry = os.path.abspath(entry)
        return {"kind": "registration + first-tool-result fallback", "entrypoint": entry,
                "present": os.path.isfile(entry),
                "registration": _contains(entry, "def register(ctx)"),
                "fallback": _contains(
                    entry, 'register_hook("transform_tool_result", _on_transform_tool_result)'
                ),
                "required": ("present", "registration", "fallback")}
    return {"kind": "unknown", "present": True, "required": ()}


def _bootstrap_valid(bootstrap):
    return all(bootstrap.get(key) for key in bootstrap.get("required", ("present",)))


def _skills():
    shipped = {}
    for root in ("skills", "skills-claude"):
        directory = os.path.join(PAYLOAD, root)
        shipped[root] = sorted(
            name for name in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, name, "SKILL.md"))
        ) if os.path.isdir(directory) else []
    return shipped


def collect(argv=()):
    harness, source = _detect_harness(argv)
    manifest_path = os.path.join(PAYLOAD, ".claude-plugin", "plugin.json")
    models_path = os.path.join(PAYLOAD, "config", "models.json")
    manifest_raw = _read_json(manifest_path)
    models_raw = _read_json(models_path)
    manifest = manifest_raw if isinstance(manifest_raw, dict) else {}
    models = models_raw if isinstance(models_raw, dict) else {}
    harnesses = models.get("harnesses")
    harnesses = harnesses if isinstance(harnesses, dict) else {}
    config = harnesses.get(harness)
    config = config if isinstance(config, dict) else {}
    bootstrap = _bootstrap(harness)
    local = _local_root()
    skills = _skills()
    skill_config = models.get("skills")
    skill_config = skill_config if isinstance(skill_config, dict) else {}
    claude_only_raw = skill_config.get("claudeOnly")
    claude_only = ({item for item in claude_only_raw if isinstance(item, str)}
                   if isinstance(claude_only_raw, list) else set())
    exclusions = skill_config.get("exclude")
    exclusions = exclusions if isinstance(exclusions, dict) else {}
    excluded_raw = exclusions.get(harness)
    excluded = ({item for item in excluded_raw if isinstance(item, str)}
                if isinstance(excluded_raw, list) else set())

    registered = [n for n in skills["skills"] if n not in excluded]
    if harness == "claude":
        registered += skills["skills-claude"]
    tiers = {}
    for tier in TIERS:
        mapping = config.get(tier)
        mapping = mapping if isinstance(mapping, dict) else {}
        tiers[tier] = {"model": mapping.get("model"), "effort": mapping.get("effort")}
    manifest_valid = _manifest_valid(manifest)
    models_valid = _models_valid(models)
    tier_mapping_valid = harness == "unknown" or all(
        isinstance(value["model"], str) and bool(value["model"])
        for value in tiers.values()
    )
    python_report = {
        "running": ".".join(str(v) for v in sys.version_info[:3]),
        "minimum": ".".join(str(v) for v in MIN_PYTHON),
        "supported": sys.version_info[:2] >= MIN_PYTHON,
    }
    breadcrumbs = _breadcrumbs()
    degraded_reasons = []
    if harness == "unknown":
        degraded_reasons.append("unknown harness")
    if "opencode-skills.log" in breadcrumbs:
        degraded_reasons.append("OpenCode namespaced skills previously failed to register")
    if not python_report["supported"]:
        degraded_reasons.append("unsupported Python")
    if not (manifest_valid and models_valid and tier_mapping_valid):
        degraded_reasons.append("invalid required payload")
    if harness != "unknown" and not _bootstrap_valid(bootstrap):
        degraded_reasons.append("missing or invalid required bootstrap")

    return {
        "harness": {"value": harness, "source": source},
        "payload": {
            "path": PAYLOAD,
            "version": manifest.get("version"),
            "manifest": manifest_path,
            "models": models_path,
            "valid": manifest_valid and models_valid and tier_mapping_valid,
        },
        "bootstrap": bootstrap,
        "python": python_report,
        "tiers": tiers,
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
        "breadcrumbs": breadcrumbs,
        # Most breadcrumbs are deliberately untrusted history. The OpenCode
        # shadow-skills log is different: it means this payload could not
        # register the namespaced skill tree, so its presence is a durable
        # degraded capability signal even though it cannot timestamp a turn.
        "status": "degraded" if degraded_reasons else "healthy",
        "degraded_reasons": degraded_reasons,
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
    state = "present" if _bootstrap_valid(boot) else "MISSING or invalid"
    row("bootstrap", f"{boot['kind']}: {state}", "disk")
    if boot.get("trust_instruction"):
        row("Codex hooks", boot["trust_instruction"], "manual check")
    python = data["python"]
    row("python", f"{python['running']} (minimum {python['minimum']})",
        "supported" if python["supported"] else "UNSUPPORTED")

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
    hermes = mem.get("hermes_projection")
    if hermes:
        row("Hermes memory", hermes["status"], hermes["path"])

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


def _exit_code(data):
    if not data["payload"].get("valid") or not data["python"].get("supported"):
        return 1
    if data["harness"]["value"] != "unknown" and not _bootstrap_valid(data["bootstrap"]):
        return 1
    return 0


def main(argv):
    data = collect(argv)
    if "--json" in argv:
        print(json.dumps(data, indent=1, sort_keys=True))
    else:
        print(_render(data))
    return _exit_code(data)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
