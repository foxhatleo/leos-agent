#!/usr/bin/env python3
"""leos-doctor — health check for an installed leos-agent (no version numbers).

Checks are read-only:
  1. linkcheck   — every executable/payload symlink in each installed tool's linkmap is present and
                   points to the expected clone source.
  2. fragment-drift — the ONE thing `git pull` cannot auto-apply: a committed merge fragment
                   (settings/config/opencode/cli-config) that changed since it was last merged.
                   Reported as "re-run leos-merge --tool X".
  3. seat-flags  — each machine-local seat file is resolved, read-only, non-persistent where
                   supported, and preserves authentication.
  4. instructions — leos's global instructions are delivered ADDITIVELY (Claude @import block,
                   OpenCode instructions[], Codex SessionStart injector) rather than a clobbering
                   symlink; a leftover clone-symlink at a retired delivery path is flagged.

A tool is checked only when Leo's installed-host registry or owned state says it was configured.
Exit 1 if any problem is found. Stdlib only.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9–3.10 local runtime fallback
    import tomli as tomllib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TOOLS = ["claude", "codex", "opencode", "cursor"]
LOCAL = os.environ.get("LEOS_LOCAL", os.path.join(REPO_ROOT, "local"))
HOME = os.path.realpath(os.path.expanduser("~"))


def current_home():
    return os.path.realpath(os.path.expanduser("~"))


def expand_path(value):
    value = value.replace("{{CODEX_HOME}}", os.environ.get("CODEX_HOME", os.path.join(current_home(), ".codex")))
    expanded = os.path.expanduser(value)
    # Resolve parent symlinks (matching leos-link._expand_dest / leos-merge.expand) but never
    # follow a final symlink, and refuse anything outside $HOME so a CODEX_HOME override can't make
    # doctor read/link-check paths outside the user's home tree.
    home = current_home()
    path = os.path.join(os.path.realpath(os.path.dirname(expanded)), os.path.basename(expanded))
    if not (path == home or path.startswith(home + os.sep)):
        print(f"refusing path outside HOME: {value}", file=sys.stderr)
        return home
    return path


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _strip_doc(d):
    return {k: v for k, v in d.items() if not str(k).startswith("$")}


def frag_sha(path, strategy):
    try:
        if strategy == "merge-toml":
            with open(path, "rb") as f:
                data = tomllib.load(f)
        else:
            data = load_json(path, None)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    text = json.dumps(_strip_doc(data), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _owned_mismatches(current, owned, path=()):
    mismatches = []
    if isinstance(owned, dict):
        if not owned:
            return []
        if not isinstance(current, dict):
            return [".".join(path) or "<root>"]
        for key, value in owned.items():
            if key not in current:
                mismatches.append(".".join(path + (str(key),)))
            else:
                mismatches.extend(_owned_mismatches(current[key], value, path + (str(key),)))
    elif isinstance(owned, list):
        if not isinstance(current, list) or any(value not in current for value in owned):
            mismatches.append(".".join(path))
    elif current != owned:
        mismatches.append(".".join(path))
    return mismatches


def _load_destination(path, strategy):
    try:
        if strategy == "merge-toml":
            with open(path, "rb") as f:
                return tomllib.load(f)
        return load_json(path, None)
    except (OSError, ValueError):
        return None


def link_state(dest, src):
    if not os.path.islink(dest) and not os.path.exists(dest):
        return "missing"
    if os.path.islink(dest):
        target = os.readlink(dest)
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(dest), target)
        if os.path.normpath(target) == os.path.normpath(src):
            return "linked" if os.path.exists(src) else "dangling"
        return "wrong-link"
    return "foreign"


def _read_text(path):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def check_instructions_delivery(tool, problems, tinfo):
    """Verify leos's global instructions are delivered ADDITIVELY (never as a clobbering symlink):
    Claude via an @import block, OpenCode via instructions[], Codex via the SessionStart injector.
    Cursor has no global instruction file, so nothing to check."""
    gi = os.path.join(REPO_ROOT, "global", "AGENTS.md")
    if tool == "claude":
        txt = _read_text("~/.claude/CLAUDE.md")
        if txt is None or "leos-agent:global-instructions" not in txt:
            problems.append("claude: @import block missing from ~/.claude/CLAUDE.md — run "
                            f"{REPO_ROOT}/bin/leos-python {REPO_ROOT}/bin/leos-block.py --tool claude")
            tinfo["instructions"] = "missing"
        elif f"@{gi}" not in txt:
            problems.append(f"claude: @import block does not point at {gi} (moved clone?) — re-run leos-block --tool claude")
            tinfo["instructions"] = "stale"
        else:
            tinfo["instructions"] = "ok"
    elif tool == "opencode":
        try:   # user-hand-editable file: a malformed opencode.json must be reported, not crash doctor
            data = load_json(os.path.expanduser("~/.config/opencode/opencode.json"), None)
        except Exception:
            data = None
        instr = data.get("instructions") if isinstance(data, dict) else None
        if not isinstance(instr, list) or gi not in instr:
            problems.append(f"opencode: instructions[] does not include {gi} — re-run leos-merge --tool opencode")
            tinfo["instructions"] = "missing"
        else:
            tinfo["instructions"] = "ok"
    elif tool == "codex":
        codex_home = os.environ.get("CODEX_HOME", os.path.join(current_home(), ".codex"))
        txt = _read_text(os.path.join(codex_home, "hooks.json"))
        if txt is None or "SessionStart" not in txt or "inject-instructions.py" not in txt:
            problems.append(f"codex: SessionStart instruction injector not registered in {codex_home}/hooks.json")
            tinfo["instructions"] = "missing"
        else:
            tinfo["instructions"] = "ok"


def check_tool(tool, configured, problems, report):
    lm = load_json(os.path.join(REPO_ROOT, "tools", tool, "linkmap.json"), {})
    home = expand_path(lm.get("toolHome", ""))
    if not configured:
        report.append({"tool": tool, "configured": False})
        return
    tinfo = {"tool": tool, "configured": True, "homePresent": bool(home and os.path.isdir(home)),
             "links": [], "merges": [], "seats": None,
             "instructions": None}

    for e in lm.get("links", []):
        dest = expand_path(e["dest"])
        src = os.path.realpath(os.path.join(REPO_ROOT, e["src"]))
        st = link_state(dest, src)
        if st != "linked":
            problems.append(f"{tool}: link {e['dest']} is '{st}' (want linked)")
        tinfo["links"].append({"dest": e["dest"], "state": st})

    # Retired symlinks: a leftover clone-symlink at an old delivery path masks the user's own
    # global file — flag it (read-only; the user removes it).
    for e in lm.get("retiredLinks", []):
        dest = expand_path(e["dest"])
        if os.path.islink(dest):
            target = os.path.realpath(dest)
            if target == REPO_ROOT or target.startswith(REPO_ROOT + os.sep):
                problems.append(f"{tool}: leftover clone-symlink at {e['dest']} — remove it "
                                f"(delivery is now additive; the symlink masks your own global file)")

    state = load_json(os.path.join(LOCAL, "merge-state.json"), {"merges": {}})
    if not isinstance(state, dict) or not isinstance(state.get("merges"), dict):
        problems.append("merge state is malformed — restore local/merge-state.json from backup or re-run setup")
        state = {"merges": {}}
    for m in lm.get("merges", []):
        merge_command = f"{REPO_ROOT}/bin/leos-python {REPO_ROOT}/bin/leos-merge.py --tool {tool}"
        cur = frag_sha(os.path.join(REPO_ROOT, m["fragment"]), m["strategy"])
        legacy = m["dest"].replace("{{CODEX_HOME}}", "~/.codex")
        entry = state["merges"].get(m["dest"], state["merges"].get(legacy, {}))
        rec = entry.get("fragmentSha")
        if rec is None:
            drift = "never-merged"
            suffix = " --package-manager <pnpm|yarn|npm>" if tool == "claude" else ""
            problems.append(f"{tool}: fragment {m['fragment']} never merged — run {merge_command}{suffix}")
        elif rec != cur:
            drift = "changed"
            problems.append(f"{tool}: fragment {m['fragment']} changed since last merge — re-run {merge_command}")
        else:
            drift = "current"
            destination = _load_destination(expand_path(m["dest"]), m["strategy"])
            mismatches = _owned_mismatches(destination, entry.get("values", {}))
            mismatches += _owned_mismatches(destination, entry.get("extraValues", {}))
            if mismatches:
                drift = "destination-drift"
                problems.append(f"{tool}: Leo-owned values missing/changed in {m['dest']}: {mismatches[:5]} — "
                                f"re-run {merge_command}")
        if tool == "claude":
            pm = entry.get("packageManager")
            recorded_pm_sha = entry.get("packageManagerSha")
            if pm:
                policy = load_json(os.path.join(REPO_ROOT, "core", "policy", "policy-data.json"), {})
                commands = policy.get("commandAllow", {}).get(pm)
                current_pm_sha = hashlib.sha256(json.dumps(commands, sort_keys=True).encode("utf-8", "replace")).hexdigest() \
                    if isinstance(commands, list) else None
                if not current_pm_sha or current_pm_sha != recorded_pm_sha:
                    drift = "package-policy-changed"
                    problems.append(f"claude: package-manager policy for {pm} changed — run "
                                    f"{merge_command} --package-manager {pm}")
        tinfo["merges"].append({"dest": m["dest"], "drift": drift})

    seats = load_json(os.path.join(LOCAL, f"seats.{tool}.json"), None)
    if seats is not None:
        seat_problems = check_seat_flags(tool, seats)
        problems.extend(f"{tool}: {p}" for p in seat_problems)
        tinfo["seats"] = "ok" if not seat_problems else "problems"
    else:
        problems.append(f"{tool}: configured host is missing local/seats.{tool}.json")
        tinfo["seats"] = "missing"

    check_instructions_delivery(tool, problems, tinfo)
    report.append(tinfo)


def _argv_of(seat):
    return [str(x) for x in seat.get("argv", [])]


EXTERNAL_PROVIDERS = {"anthropic", "openai", "zhipu", "google", "xai", "custom"}


def _is_opus_line(model):
    """The Anthropic seat rule from AGENTS.md, made mechanical: an Opus-line id (or the `opus`
    alias) — never the Claude-5/Mythos-class line (Fable, Mythos)."""
    m = model.lower()
    concrete = re.fullmatch(r"(?:[a-z0-9._-]+/)*claude-opus-[a-z0-9][a-z0-9._-]*", m)
    return m == "opus" or bool(concrete and "fable" not in m and "mythos" not in m)


def check_seat_flags(tool, seats):
    problems = []
    if not isinstance(seats, dict):
        return ["seats file must be a JSON object"]
    if seats.get("host") not in (None, tool):
        problems.append(f"seats file host is {seats.get('host')!r}, expected {tool!r}")
    external = seats.get("seats", [])
    if not isinstance(external, list):
        return problems + ["seats must be an array"]
    native = seats.get("native")
    if not isinstance(native, dict) or native.get("mode") not in ("subagent", "exec"):
        return problems + ["native seat must be an object with mode subagent or exec"]
    if native["mode"] == "subagent" and not isinstance(native.get("model"), str):
        problems.append("native subagent seat requires a model")
    elif native["mode"] == "subagent":
        model = native["model"]
        unresolved = sorted(set(re.findall(r"\{[A-Z][A-Z0-9_]*\}", model)))
        if unresolved:
            problems.append(f"native subagent model has unresolved placeholders {unresolved}: {model!r}")
        elif tool == "claude" and not _is_opus_line(model):
            problems.append(
                f"claude native subagent must be the Opus line (never Fable/Mythos), got {model!r}")
    if native["mode"] == "exec" and not _argv_of(native):
        problems.append("native exec seat requires argv")
    seen = set()
    all_seats = []
    for seat in external:
        if not isinstance(seat, dict):
            problems.append("external seat must be an object")
            continue
        name = seat.get("name")
        if not isinstance(name, str) or not name:
            problems.append("external seat missing name")
        elif name in seen:
            problems.append(f"duplicate external seat name: {name}")
        else:
            seen.add(name)
        provider = seat.get("provider")
        if provider not in EXTERNAL_PROVIDERS:
            problems.append(
                f"external seat {name or '<unnamed>'} provider must be one of "
                + ", ".join(sorted(EXTERNAL_PROVIDERS)))
        if name == "opus" and provider != "anthropic":
            problems.append("external opus role must declare provider 'anthropic'")
        if not _argv_of(seat):
            problems.append(f"external seat {name or '<unnamed>'} requires argv")
        if seat.get("transport") not in ("stdin", "arg"):
            problems.append(f"external seat {name or '<unnamed>'} transport must be stdin or arg")
        env = seat.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                            for k, v in env.items()):
            problems.append(f"external seat {name or '<unnamed>'} env must be string:string")
        all_seats.append(seat)
    if isinstance(native, dict):
        all_seats = all_seats + [native]
    for seat in all_seats:
        label = seat.get("name") or ("native" if seat is native else "<unnamed>")
        timeout = seat.get("timeoutSeconds", 300)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 900:
            problems.append(f"seat {label} timeoutSeconds must be 1..900")
        plan_timeout = seat.get("planTimeoutSeconds")
        if plan_timeout is not None and (not isinstance(plan_timeout, int) or
                                         isinstance(plan_timeout, bool) or not 1 <= plan_timeout <= 900):
            problems.append(f"seat {label} planTimeoutSeconds must be 1..900")
        argv = _argv_of(seat)
        if not argv:
            continue
        base = os.path.basename(argv[0])
        joined = " ".join(argv)
        # {PROMPT_TEXT} and {EFFORT} are RUNTIME placeholders the runner substitutes at dispatch
        # (runner.prepare_command); every other leftover token — {MODEL} above all — means setup
        # skipped its resolution step.
        unresolved = sorted(set(re.findall(r"\{[A-Z][A-Z0-9_]*\}", joined))
                            - {"{PROMPT_TEXT}", "{EFFORT}"})
        if unresolved:
            problems.append(f"seat has unresolved placeholders {unresolved}: {joined}")
        if base == "claude" and ("--safe-mode" not in argv or not _option_is(argv, "--permission-mode", "plan")):
            problems.append(f"claude seat missing --safe-mode or --permission-mode plan: {joined}")
        if base == "claude" and "--no-session-persistence" not in argv:
            problems.append(f"claude seat persists sessions: {joined}")
        if base == "codex" and not _option_is(argv, "--sandbox", "read-only"):
            problems.append(f"codex seat not --sandbox read-only: {joined}")
        if base == "codex" and "--ephemeral" not in argv:
            problems.append(f"codex seat persists sessions: {joined}")
        if base == "codex" and isinstance(seat.get("env"), dict) and seat["env"].get("CODEX_HOME"):
            problems.append(f"codex seat overrides CODEX_HOME and may lose host authentication: {joined}")
        if base == "opencode" and not _option_is(argv, "--agent", "plan"):
            problems.append(f"opencode seat missing --agent plan: {joined}")
        if base == "cursor-agent" and not _option_is(argv, "--mode", "plan"):
            problems.append(f"cursor-agent seat not --mode plan: {joined}")
        if base == "cursor-agent" and seat.get("adapter") != "cursor-json":
            problems.append(f"cursor-agent seat needs adapter cursor-json after a setup output-contract smoke test: {joined}")
        if base == "cursor-agent":
            response_path = seat.get("responsePath")
            if not isinstance(response_path, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", response_path):
                problems.append(f"cursor-agent seat needs a simple responsePath to reviewer text: {joined}")
        # Provider identity—not a freely chosen display name—makes the standing Anthropic rule
        # mechanical across direct Claude and Cursor/OpenRouter fallback transports. The direct
        # claude binary remains defense-in-depth even for an invalid/missing provider field.
        if base == "claude" or seat.get("provider") == "anthropic" or seat.get("name") == "opus":
            model = ""
            for i, value in enumerate(argv):
                if value in ("--model", "-m") and i + 1 < len(argv):
                    model = argv[i + 1]
            if not model:
                problems.append(f"Anthropic seat must pin --model to an Opus-line id: {joined}")
            elif not _is_opus_line(model):
                problems.append(f"Anthropic seat must use the Opus line (never Fable/Mythos), got {model!r}")
    return problems


def _option_is(argv, flag, wanted):
    for i, value in enumerate(argv):
        if value == flag and i + 1 < len(argv) and argv[i + 1] == wanted:
            return True
        if value == f"{flag}={wanted}":
            return True
    return False


def configured_hosts():
    """Read the explicit link-time registry, with a read-only legacy migration heuristic.

    A directory such as ~/.claude is not evidence that Leo configured it; old installs that
    predate the registry are recognized only when they have a Leo seat, merge record, or link.
    """
    data = load_json(os.path.join(LOCAL, "installed-hosts.json"), {})
    hosts = data.get("hosts", []) if isinstance(data, dict) else []
    hosts = {h for h in hosts if h in TOOLS}
    state = load_json(os.path.join(LOCAL, "merge-state.json"), {"merges": {}})
    merges = state.get("merges", {}) if isinstance(state, dict) else {}
    for tool in TOOLS:
        if os.path.exists(os.path.join(LOCAL, f"seats.{tool}.json")):
            hosts.add(tool)
            continue
        lm = load_json(os.path.join(REPO_ROOT, "tools", tool, "linkmap.json"), {})
        # A shared cross-host link (currently ~/.agents/skills/council) is installed once and
        # appears in several host link maps. It cannot prove which of those hosts was configured.
        if any(not e.get("shared") and os.path.islink(expand_path(e["dest"]))
               for e in lm.get("links", [])):
            hosts.add(tool)
            continue
        if any(m["dest"] in merges or m["dest"].replace("{{CODEX_HOME}}", "~/.codex") in merges
               for m in lm.get("merges", [])):
            hosts.add(tool)
    return hosts


def check_runtime(configured, problems):
    """Validate the clone-private runtime without invoking an ambient Python."""
    requirements = os.path.join(REPO_ROOT, "requirements", "runtime.txt")
    state_path = os.path.join(LOCAL, "runtime-state.json")
    launcher = os.path.join(LOCAL, ".venv", "bin", "python")
    try:
        with open(requirements, "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        want = None
    state = load_json(state_path, {})
    runtime_report = {}
    if os.path.isfile(launcher):
        try:
            proc = subprocess.run([launcher, os.path.join(REPO_ROOT, "bin", "leos-runtime.py"), "status"],
                                  capture_output=True, text=True, timeout=45)
            runtime_report = json.loads(proc.stdout) if proc.stdout else {}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            runtime_report = {}
    ok = bool(want and state.get("requirementsSha") == want and runtime_report.get("ok"))
    if configured and not ok:
        problems.append("runtime: local/.venv is missing or requirements changed — run "
                        f"python3 {REPO_ROOT}/bin/leos-runtime.py setup --refresh")
    return {"component": "runtime", "ok": ok, "venvPython": launcher,
            "requirementsSha": want, "installedRequirementsSha": state.get("requirementsSha"),
            "health": runtime_report}


def legacy_council_state_report():
    """Surface, but never auto-import, the prior global council state location."""
    legacy = os.path.join(current_home(), ".local", "state", "leos-agent", "council", "state")
    target = os.path.join(LOCAL, "council", "state")
    available = os.path.isdir(legacy) and os.path.realpath(legacy) != os.path.realpath(target)
    return {"component": "legacyCouncilState", "present": available, "source": legacy,
            "target": target,
            "migrationCommand": f"{REPO_ROOT}/bin/leos-python {REPO_ROOT}/core/council/bin/council.py migrate-legacy-state"
            if available else None}


def main():
    problems, report = [], []
    configured = configured_hosts()
    report.append(check_runtime(configured, problems))
    report.append(legacy_council_state_report())
    for tool in TOOLS:
        check_tool(tool, tool in configured, problems, report)
    print(json.dumps({"ok": not problems, "problems": problems, "report": report}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
