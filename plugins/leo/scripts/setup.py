#!/usr/bin/env python3
"""setup: turn on the wiring a plugin install cannot turn on for itself.

Everything here is opt-in and idempotent. Nothing in this file runs at
install or session start — the harnesses' own plugin systems have no
install-time hook to hang it on (Hermes' register() only runs at session
start), so consent is asked for once, explicitly, and recorded in
machine-local state.

The one thing it enables today is Hermes memory projection. The other four
harnesses project into a file whose whole purpose is user instructions;
Hermes' only user-owned global file is SOUL.md, its agent-identity prompt and
the opening section of every system prompt on the machine. That is a blast
radius worth a deliberate yes.

  setup.py                     report what is on and what is available
  setup.py --json              the same facts as JSON
  setup.py enable <feature>    turn one on
  setup.py disable <feature>   turn it off again

Features: hermes-memory

`setup.py apply` is a separate, ACTION layer: it bootstraps the MCP servers
`config/models.json`'s "mcp" section declares as core for whichever harness is
actually running this script (detected the same way scripts/doctor.py does —
see _detect_harness there, reused rather than re-implemented), writing only
that harness's own config, read-modify-write, idempotently. `--dry-run` prints
the exact commands or diffs and touches nothing.

  setup.py apply                install this harness's core MCP servers
  setup.py apply --dry-run      print what would happen; execute nothing
  setup.py apply --harness X    state the harness instead of detecting it

An unsupported (or undetectable) harness refuses outright: nothing is
touched, exit code is non-zero. Vendor connectors (Slack, Sentry, Linear...)
live in the same "mcp" config under "connectors" but apply never installs
them — see the next layer.

`setup.py connectors` is that separate, READ-ONLY layer over the same
"connectors" list. It reports which are already registered against the
running harness — matching by endpoint URL first, then by name, because a
claude.ai connector such as Gmail or Vercel is registered against the
account and never written to `~/.claude.json` at all; only `claude mcp list`
can see it — and which are not. `setup.py connect <key>...` installs one or
more of the not-yet-registered ones. Every connector is OAuth, so a
successful install is reported `needs-auth`: setup never handles a
credential, and the harness runs the browser consent flow on first use.
Snowflake's endpoint embeds org/account/database/schema and cannot be
guessed — `connect snowflake` without `--url <URL>` refuses outright,
writing nothing.

  setup.py connectors               list installed vs. available connectors
  setup.py connectors --json        the same facts as JSON, for a question tool
  setup.py connect <key> [<key>...] install one or more connectors
  setup.py connect <key> --url URL  supply an account-specific URL (snowflake)

Nothing here is offered automatically. `connectors`/`connectors --json` never
write anything, on any harness, including an unsupported one. The multi-select
prompt itself — and the "install nothing unless asked" default when no
question tool exists — is the calling skill's job, not this script's; see
skills/setup/SKILL.md.

Exit code is 0 on success, 1 on an unknown feature, a failed write, or an
apply/connect that refused (unsupported harness, or a connector with no URL
on file and none supplied).
"""
import json
import os
import re
import shlex
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import doctor  # noqa: E402  (path fix must precede the import)
import memory  # noqa: E402
import state  # noqa: E402

FEATURES = {
    "hermes-memory": {
        "state": ("hermes", "projectMemory"),
        "summary": "project global memory facts into $HERMES_HOME/SOUL.md",
    },
}


def _read():
    return state.load(state.state_file(memory.SETUP_STATE))


def _write(feature, value):
    section, key = FEATURES[feature]["state"]
    path = state.state_file(memory.SETUP_STATE)
    # Same lock the state CLI takes, so a concurrent merge cannot lose this.
    with state._locked(path):
        data = state.deep_merge(state.load(path), {section: {key: value}})
        state.atomic_write(path, data)


def _enabled(data, feature):
    section, key = FEATURES[feature]["state"]
    return bool((data.get(section) or {}).get(key))


def _hermes_facts():
    """What projection would actually do right now, without doing it."""
    home = memory.hermes_home()
    soul = os.path.join(home, "SOUL.md")
    return {
        "home": home,
        "soul": soul,
        "home_exists": os.path.isdir(home),
        "soul_exists": os.path.isfile(soul),
        "projected": os.path.isfile(soul) and memory.BEGIN in _slurp(soul),
    }


def _slurp(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def collect():
    data = _read()
    return {
        "state_file": state.state_file(memory.SETUP_STATE),
        "features": {
            name: {"enabled": _enabled(data, name), "summary": spec["summary"]}
            for name, spec in FEATURES.items()
        },
        "hermes": _hermes_facts(),
    }


def _render(data):
    lines = ["leo setup", ""]
    for name, info in sorted(data["features"].items()):
        lines.append(f"  {'on ' if info['enabled'] else 'off'}  {name}  —  {info['summary']}")
    lines.append("")

    hermes = data["hermes"]
    if data["features"]["hermes-memory"]["enabled"]:
        if not hermes["home_exists"]:
            lines.append(f"  Hermes is enabled but {hermes['home']} does not exist, so nothing")
            lines.append("  is written. That is the not-installed case, not a fault.")
        elif not hermes["soul_exists"]:
            lines.append(f"  Hermes is enabled but {hermes['soul']} does not exist.")
            lines.append("  Leo never creates it: Hermes falls back to a built-in persona when")
            lines.append("  the file is absent, so creating it would replace your agent's")
            lines.append("  identity. Write the file yourself and Leo will splice into it.")
        elif hermes["projected"]:
            lines.append(f"  Hermes: projecting into {hermes['soul']}")
        else:
            lines.append(f"  Hermes: enabled, {hermes['soul']} present, not yet written")
            lines.append("  (projection runs at the next session start).")
    else:
        lines.append("  Hermes memory projection is off. Turn it on with:")
        lines.append("    setup.py enable hermes-memory")
        lines.append("")
        lines.append("  It splices a marked block into $HERMES_HOME/SOUL.md, which is the")
        lines.append("  opening section of every Hermes system prompt. Everything outside")
        lines.append("  Leo's markers is preserved byte for byte, one .leo-backup is taken")
        lines.append("  before the first write, and the file is never created.")
    lines.append("")
    lines.append(f"  state: {data['state_file']}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# apply: bootstrap the running harness's own MCP config
# --------------------------------------------------------------------------
#
# Everything below reads and writes exactly one harness's own config — the
# one scripts/doctor.py's _detect_harness (reused, not re-implemented; see
# tests/test_doctor.py:93-152) says is actually running this script. "Already
# installed" is always answered by reading that config fresh, never by a flag
# in state.json — a private flag drifts the moment a user removes a server by
# hand, and the whole point of this layer is that re-running it is safe.

STATUS_LABELS = {
    "already-present": "already-present",
    "installed": "installed-now",
    "would-install": "would-install (dry run)",
    "needs-auth": "needs-auth",
    "manual": "manual-steps",
    "failed": "FAILED",
    "error": "error",
}
_STATUS_ORDER = ("already-present", "installed", "would-install", "needs-auth",
                 "manual", "failed", "error")


def _mcp_config():
    models = doctor._read_json(os.path.join(doctor.PAYLOAD, "config", "models.json")) or {}
    return models.get("mcp") or {}


def _canonical_roles():
    agents = doctor._read_json(os.path.join(doctor.PAYLOAD, "adapters", "opencode", "agents.json")) or {}
    # OpenCode's built-in `build` agent is a canonical gating target even
    # though Leo does not ship an override for it in agents.json.
    return {f"leo-{name}" for name in agents} | {"build"}


def _validate_mcp_config(mcp):
    """Return human-readable catalogue errors; never guess past bad policy.

    The catalogue drives commands that can modify a user's configuration.  A
    partial or typo-tolerant read is therefore unsafe: validate it before
    rendering an actionable plan as well as before applying one.
    """
    errors = []
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    if not isinstance(servers, dict) or not servers:
        return ["servers must be a nonempty object"]
    allowed_auth = {"none", "oauth", "manual-oauth"}
    for key, spec in servers.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(spec, dict):
            errors.append("server keys and values must be nonempty strings and objects")
            continue
        if spec.get("transport") not in ("stdio", "http"):
            errors.append(f"server {key}: transport must be stdio or http")
        if spec.get("auth") not in allowed_auth:
            errors.append(f"server {key}: invalid auth")
        if spec.get("registration") not in ("automatic", "manual"):
            errors.append(f"server {key}: registration must be automatic or manual")
        prerequisites = spec.get("prerequisites")
        if (not isinstance(prerequisites, list) or not prerequisites or
                not all(isinstance(item, str) and item.strip() for item in prerequisites)):
            errors.append(f"server {key}: prerequisites must be a nonempty string array")
        command = spec.get("command")
        if spec.get("transport") == "stdio" and (not isinstance(command, list) or
                                                   not command or
                                                   not all(isinstance(part, str) and part for part in command)):
            errors.append(f"server {key}: stdio command must be a nonempty string array")
        if isinstance(command, list) and any("@latest" in part for part in command):
            errors.append(f"server {key}: commands must use reviewed exact versions, never @latest")
        exact = spec.get("exactVersion")
        if not isinstance(exact, str) or not exact or exact not in (command or []):
            errors.append(f"server {key}: exactVersion must match one pinned command argument")

    core = mcp.get("core")
    if not isinstance(core, dict):
        errors.append("core must be an object")
    else:
        for harness, refs in core.items():
            if harness == "_comment":
                continue
            if not isinstance(refs, list) or any(ref not in servers for ref in refs):
                errors.append(f"core {harness}: every entry must reference a server")

    gating = mcp.get("gating") or {}
    opencode = gating.get("opencode") if isinstance(gating, dict) else None
    if opencode is not None:
        for ref in opencode.get("off", []):
            if ref not in servers:
                errors.append(f"gating opencode off: unknown server {ref}")
        valid_agents = _canonical_roles()
        agents = opencode.get("agents", {})
        if not isinstance(agents, dict):
            errors.append("gating opencode agents must be an object")
        else:
            for agent, refs in agents.items():
                if agent not in valid_agents:
                    errors.append(f"gating opencode: unknown agent {agent}")
                if not isinstance(refs, list) or any(ref not in servers for ref in refs):
                    errors.append(f"gating opencode {agent}: every entry must reference a server")

    connectors = mcp.get("connectors") or []
    if not isinstance(connectors, list):
        errors.append("connectors must be an array")
    else:
        seen, endpoints = set(), set()
        for connector in connectors:
            key = connector.get("key") if isinstance(connector, dict) else None
            if not isinstance(key, str) or not key.strip() or key in seen:
                errors.append("connector keys must be unique and nonempty")
                continue
            seen.add(key)
            if connector.get("transport") != "http":
                errors.append(f"connector {key}: transport must be http")
            if connector.get("auth") not in allowed_auth:
                errors.append(f"connector {key}: invalid auth")
            if connector.get("registration") not in ("automatic", "manual"):
                errors.append(f"connector {key}: registration must be automatic or manual")
            prerequisites = connector.get("prerequisites")
            if (not isinstance(prerequisites, list) or not prerequisites or
                    not all(isinstance(item, str) and item.strip() for item in prerequisites)):
                errors.append(f"connector {key}: prerequisites must be a nonempty string array")
            endpoint = connector.get("url")
            if not isinstance(endpoint, str) or not endpoint.strip():
                endpoint = connector.get("urlTemplate")
                if not (connector.get("registration") == "manual" and isinstance(endpoint, str) and endpoint.strip()):
                    errors.append(f"connector {key}: endpoint must be nonempty")
            if isinstance(endpoint, str) and endpoint.strip() and endpoint in endpoints:
                errors.append(f"connector {key}: endpoint must be unique")
            elif isinstance(endpoint, str) and endpoint.strip():
                endpoints.add(endpoint)
    return errors


def _catalogue_or_error():
    mcp = _mcp_config()
    errors = _validate_mcp_config(mcp)
    if errors:
        print("leo setup: invalid MCP catalogue; refusing to render or write:")
        for error in errors:
            print("  - " + error)
        return None
    return mcp


def _core_servers(mcp, harness):
    names = (mcp.get("core") or {}).get(harness) or []
    catalogue = mcp.get("servers") or {}
    return [(name, catalogue[name]) for name in names if name in catalogue]


def _harness_dir(harness):
    """Where each harness's own config lives.

    Same env-var precedence memory.py's projection_targets() already
    established (CLAUDE_CONFIG_DIR, CODEX_HOME, XDG_CONFIG_HOME) — repeated
    here as plain one-line lookups rather than imported, because each is a
    single env-var read with no branch order to drift on. That is unlike
    harness DETECTION, whose multi-branch ordering (cursor before codex,
    absence-of-CLAUDE_PLUGIN_ROOT is not a codex signal, ...) is exactly what
    scripts/doctor.py delegates to hooks/session-start.py rather than
    re-deriving, and what apply reuses via doctor._detect_harness() below.

    Its absence means the harness is not installed: apply must never create
    it (memory.py:403-430's rule, mirrored here for the same reason).
    """
    home = os.path.expanduser("~")
    if harness == "claude":
        return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    if harness == "codex":
        return os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")
    if harness == "cursor":
        return os.path.join(home, ".cursor")
    if harness == "opencode":
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        return os.path.join(xdg, "opencode")
    if harness == "hermes":
        return memory.hermes_home()
    return None


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _entry(name, spec, status, detail="", command=None, diff=None):
    return {
        "kind": "server", "server": name, "label": spec.get("label", name),
        "status": status, "detail": detail, "command": command, "diff": diff,
    }


def _gate_entry(label, status):
    return {
        "kind": "gate", "server": None, "label": label, "status": status,
        "detail": "", "command": None, "diff": None,
    }


def _no_gate_note(harness, gate):
    return {
        "kind": "note", "server": None, "label": f"{harness} config directory",
        "status": "manual",
        "detail": (f"{gate} does not exist. leo never creates a harness config "
                   "directory — its absence means the harness is not installed, "
                   "so nothing was touched."),
        "command": None, "diff": None,
    }


def _broken_note(harness, path, detail=""):
    return {
        "kind": "note", "server": None, "label": f"{harness} config file",
        "status": "error",
        "detail": detail or f"{path} could not be read in the expected shape; refusing to touch it.",
        "command": None, "diff": None,
    }


def _shell_install(name, spec, cmd, timeout=60):
    """Run an install command for real. Never called from a dry run."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return _entry(name, spec, "failed", command=cmd,
                      detail=f"could not run {cmd[0]!r}: {exc}")
    if result.returncode != 0:
        # `failed`, not `manual`: the command ran and refused. Hermes and
        # Claude-in-Chrome reach `manual` having correctly done nothing, and
        # exit 0 is right for them; a install that actually failed has to be
        # visible to a caller chaining on `&&`.
        detail = (result.stderr or result.stdout or "").strip()[:400]
        return _entry(name, spec, "failed", command=cmd, detail=detail)
    if spec.get("auth") not in (None, "none"):
        return _entry(name, spec, "needs-auth", command=cmd, detail=spec.get("authNote", ""))
    return _entry(name, spec, "installed", command=cmd)


# ---- claude --------------------------------------------------------------

def _claude_json_path():
    # ~/.claude.json is a sibling of the ~/.claude settings directory, not a
    # file inside it — confirmed against this machine's own ~/.claude.json,
    # which already carries a top-level "mcpServers" key. CLAUDE_CONFIG_DIR
    # is still honored for both, since it is documented to relocate the
    # whole config surface, not just the settings directory.
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~")
    return os.path.join(base, ".claude.json")


def _apply_claude(servers, dry_run):
    gate = _harness_dir("claude")
    if not os.path.isdir(gate):
        return [_no_gate_note("claude", gate)]
    path = _claude_json_path()
    data = _read_json_file(path)
    if data is None and os.path.exists(path):
        return [_broken_note("claude", path)]
    data = data or {}
    have = set((data.get("mcpServers") or {}).keys()) if isinstance(data, dict) else set()
    entries = []
    for name, spec in servers:
        cmd = ["claude", "mcp", "add", "--scope", "user", name, "--", *spec["command"]]
        if name in have:
            entries.append(_entry(name, spec, "already-present", command=cmd))
        elif dry_run:
            entries.append(_entry(name, spec, "would-install", command=cmd))
        else:
            entries.append(_shell_install(name, spec, cmd))
    return entries


# ---- codex -----------------------------------------------------------------

def _codex_core_names():
    try:
        result = subprocess.run(["codex", "mcp", "list", "--json"],
                                 capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    return {row.get("name") for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)}


def _apply_codex(servers, dry_run):
    gate = _harness_dir("codex")
    if not os.path.isdir(gate):
        return [_no_gate_note("codex", gate)]
    present_names = _codex_core_names()
    if present_names is None:
        return [_broken_note("codex", gate, "could not determine Codex MCP state; refusing to write blindly")]
    entries = []
    for name, spec in servers:
        cmd = ["codex", "mcp", "add", name, "--", *spec["command"]]
        if name in present_names:
            entries.append(_entry(name, spec, "already-present", command=cmd))
        elif dry_run:
            entries.append(_entry(name, spec, "would-install", command=cmd))
        else:
            entries.append(_shell_install(name, spec, cmd))
    return entries


def _codex_features():
    try:
        result = subprocess.run(["codex", "features", "list"],
                                 capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    features = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, enabled = parts[0], parts[-1]
        stage = " ".join(parts[1:-1])
        features[name] = {"stage": stage, "enabled": enabled == "true"}
    return features


def _codex_web_search_mode():
    """The effective, persistent value — never the per-invocation --search flag.

    `tools.web_search` and `features.web_search_request` are both deprecated
    (confirmed against `codex features list` on this machine: both show
    `deprecated false`). The live key is the top-level `web_search` scalar in
    config.toml, default "cached" when absent. Read-only — writing it back is
    a different matter and apply never does that (SAFETY RULES / harness
    toggles: report, never flip).

    A narrow read-only parser keeps this available on the supported Python
    3.9 floor without taking a TOML dependency. It recognizes only this one
    top-level scalar and treats anything ambiguous as unknown.
    """
    path = os.path.join(_harness_dir("codex"), "config.toml")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return "cached"
    except OSError:
        return None
    # setup needs precisely one top-level scalar, not a TOML implementation.
    # Reject duplicate/malformed assignments rather than trying to be clever:
    # unreadable means unknown and must never license a write.
    matches = re.findall(r'^\s*web_search\s*=\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*(?:#.*)?$',
                         text, flags=re.MULTILINE)
    mentions = re.findall(r'^\s*web_search\s*=', text, flags=re.MULTILINE)
    if not mentions:
        return "cached"
    if len(matches) != 1 or len(mentions) != 1:
        return None
    return matches[0]


def _codex_toggles():
    entries = []
    features = _codex_features()
    computer_use = (features or {}).get("computer_use")
    if computer_use is not None:
        entries.append({
            "name": "computer_use",
            "status": "already-on" if computer_use["enabled"] else "off",
            "detail": f"stage={computer_use['stage']}",
        })
    else:
        entries.append({"name": "computer_use", "status": "unknown",
                         "detail": "could not run `codex features list`"})

    mode = _codex_web_search_mode()
    if mode is None:
        entries.append({
            "name": "web_search",
            "status": "unknown",
            "detail": ("config.toml is unreadable or its top-level web_search value is "
                       "malformed or ambiguous; no change will be inferred from it."),
        })
    elif mode == "live":
        entries.append({"name": "web_search", "status": "already-live", "detail": f"mode={mode!r}"})
    else:
        config_path = os.path.join(_harness_dir("codex"), "config.toml")
        entries.append({
            "name": "web_search",
            "status": "offer",
            "detail": (f"currently {mode!r} (default). \"live\" grants the model unrestricted, "
                       "unapproved live web retrieval — a security-relevant change apply never "
                       f"makes for you. To take it, add `web_search = \"live\"` to {config_path} "
                       "yourself."),
        })
    return entries


# ---- claude toggle (Claude in Chrome) --------------------------------------

def _claude_toggles():
    # No config key exists anywhere for this — it is a Claude Desktop UI
    # toggle with no CLI or file surface to probe, so this never claims a
    # state it cannot see. NEVER report it as enabled.
    return [{
        "name": "claude-in-chrome",
        "status": "manual",
        "detail": ("no config key — enable it yourself: Settings -> Connectors -> "
                   "Claude in Chrome. Check with the /chrome slash command."),
    }]


_TOGGLES = {
    "codex": _codex_toggles,
    "claude": _claude_toggles,
}


# ---- cursor ------------------------------------------------------------

def _apply_cursor(servers, dry_run):
    gate = _harness_dir("cursor")
    if not os.path.isdir(gate):
        return [_no_gate_note("cursor", gate)]
    path = os.path.join(gate, "mcp.json")
    data = _read_json_file(path)
    if data is None and os.path.exists(path):
        return [_broken_note("cursor", path)]
    if not isinstance(data, dict):
        data = {}
    existing = data.get("mcpServers")
    existing = dict(existing) if isinstance(existing, dict) else {}
    entries = []
    changed = False
    for name, spec in servers:
        if name in existing:
            entries.append(_entry(name, spec, "already-present"))
            continue
        shape = {"command": spec["command"][0], "args": list(spec["command"][1:]), "env": {}}
        if dry_run:
            entries.append(_entry(name, spec, "would-install",
                                   diff=json.dumps({name: shape}, indent=2)))
            continue
        existing[name] = shape
        changed = True
        if spec.get("auth") not in (None, "none"):
            entries.append(_entry(name, spec, "needs-auth", detail=spec.get("authNote", "")))
        else:
            entries.append(_entry(name, spec, "installed"))
    if changed:
        data["mcpServers"] = existing
        _write_config(path, json.dumps(data, indent=2) + "\n")
    return entries


def _write_config(path, text):
    """Back up and write a harness config, following a symlink to its target.

    A dotfiles-managed config is a symlink into the repo that owns it. Writing
    the link path directly would replace the link with a regular file, orphan
    the real file, and leave the .leo-backup sitting next to a link that no
    longer points anywhere useful — a silent, confusing loss of the user's own
    setup. memory.py:_project_one already resolves realpath before splicing
    for exactly this reason; do the same here.

    The existing mode is carried across too. A harness config can legitimately
    be 0600 — it may hold an API key in an `env` block — and _atomic_text
    defaults to 0644, so writing without this would quietly widen the
    permissions on the one file most likely to hold a secret.
    """
    target = os.path.realpath(path)
    mode = 0o644
    try:
        mode = os.stat(target).st_mode & 0o777
    except OSError:
        pass  # New file: _atomic_text's own default is the right answer.
    memory._backup_once(target)
    memory._atomic_text(target, text, mode)


# ---- opencode ------------------------------------------------------------


def _opencode_config_path(gate):
    explicit = os.environ.get("OPENCODE_CONFIG")
    if explicit:
        return os.path.expanduser(explicit)
    jsonc = os.path.join(gate, "opencode.jsonc")
    plain = os.path.join(gate, "opencode.json")
    if os.path.exists(plain) and os.path.exists(jsonc):
        raise ValueError("both opencode.jsonc and opencode.json exist; refusing to choose")
    if os.path.exists(plain):
        return plain
    return jsonc  # default target for a brand-new file, per the brief


# JSONC parsing and add-only modification are delegated to the pinned,
# vendored jsonc-parser@3.3.1 UMD runtime. A missing Node runtime is unknown,
# never permission to rewrite a config through a fallback parser.
_JSONC_BRIDGE = os.path.join(_HERE, "jsonc_bridge.cjs")
_JSONC_RUNTIME = os.path.join(os.path.dirname(_HERE), "vendor", "jsonc-parser-3.3.1", "lib", "umd", "main.js")


def _jsonc_bridge(payload):
    try:
        result = subprocess.run(["node", _JSONC_BRIDGE, _JSONC_RUNTIME], input=json.dumps(payload),
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if result.returncode:
        return None, (result.stderr or result.stdout or "jsonc bridge failed").strip()
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError:
        return None, "jsonc bridge returned invalid data"


def _jsonc_parse(text):
    result, error = _jsonc_bridge({"mode": "parse", "text": text})
    return (None, error) if error else (result.get("value"), None)


def _jsonc_missing_edits(before, after, path=()):
    edits = []
    for key, value in after.items():
        if key not in before:
            edits.append({"path": list(path + (key,)), "value": value})
        elif isinstance(before[key], dict) and isinstance(value, dict):
            edits.extend(_jsonc_missing_edits(before[key], value, path + (key,)))
    return edits


def _jsonc_apply_missing(text, before, after):
    result, error = _jsonc_bridge({"mode": "modify", "text": text,
                                   "edits": _jsonc_missing_edits(before, after)})
    return (None, error) if error else (result.get("text"), None)


def _opencode_shape_error(data, gating=None):
    """Return the first unsafe OpenCode object shape, if any.

    The lossless editor intentionally adds missing keys only. Treating a
    scalar where an object is required as an empty object would therefore
    produce a no-op edit while reporting success. A syntactically valid but
    structurally invalid config is unknown state, so refuse it instead.
    """
    if "mcp" in data and not isinstance(data["mcp"], dict):
        return "mcp must be an object"
    for name, entry in (data.get("mcp") or {}).items():
        if not isinstance(entry, dict):
            return f"mcp.{name} must be an object"
    if not gating:
        return None
    if gating.get("off") and "tools" in data and not isinstance(data["tools"], dict):
        return "tools must be an object"
    if gating.get("agents") and "agent" in data and not isinstance(data["agent"], dict):
        return "agent must be an object"
    agents = data.get("agent") or {}
    for name in (gating.get("agents") or {}):
        if name not in agents:
            continue
        entry = agents[name]
        if not isinstance(entry, dict):
            return f"agent.{name} must be an object"
        if "tools" in entry and not isinstance(entry["tools"], dict):
            return f"agent.{name}.tools must be an object"
    return None


def _apply_opencode_gating(data, gating, dry_run):
    """Write mcp.gating.opencode's tool gates, but only where a key is wholly
    absent. A key that already exists — Leo's own earlier write, or a value
    the user chose themselves — is left alone and reported already-present:
    apply never overwrites a user's other config, and a previous run's own
    gate is exactly that from apply's point of view."""
    entries = []
    changed = False

    off = gating.get("off") or []
    if off:
        tools = data.get("tools")
        tools = dict(tools) if isinstance(tools, dict) else {}
        for name in off:
            key = f"{name}*"
            label = f'tools["{key}"] = false'
            if key in tools:
                entries.append(_gate_entry(label, "already-present"))
            elif dry_run:
                entries.append(_gate_entry(label, "would-install"))
            else:
                tools[key] = False
                changed = True
                entries.append(_gate_entry(label, "installed"))
        if changed:
            data["tools"] = tools

    for agent_name, names in (gating.get("agents") or {}).items():
        if not names:
            continue
        agents = data.get("agent")
        agents = dict(agents) if isinstance(agents, dict) else {}
        agent_entry = agents.get(agent_name)
        agent_entry = dict(agent_entry) if isinstance(agent_entry, dict) else {}
        agent_tools = agent_entry.get("tools")
        agent_tools = dict(agent_tools) if isinstance(agent_tools, dict) else {}
        agent_changed = False
        for name in names:
            key = f"{name}*"
            label = f'agent["{agent_name}"].tools["{key}"] = true'
            if key in agent_tools:
                entries.append(_gate_entry(label, "already-present"))
            elif dry_run:
                entries.append(_gate_entry(label, "would-install"))
            else:
                agent_tools[key] = True
                agent_changed = True
                entries.append(_gate_entry(label, "installed"))
        if agent_changed:
            agent_entry["tools"] = agent_tools
            agents[agent_name] = agent_entry
            data["agent"] = agents
            changed = True

    return changed, entries


def _apply_opencode(servers, gating, dry_run):
    gate = _harness_dir("opencode")
    if not os.path.isdir(gate):
        return [_no_gate_note("opencode", gate)]
    try:
        path = _opencode_config_path(gate)
    except ValueError as exc:
        return [_broken_note("opencode", gate, detail=str(exc))]
    raw = ""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            return [_broken_note("opencode", path, detail=str(exc))]
        data, error = _jsonc_parse(raw)
        if error:
            return [_broken_note("opencode", path, detail=f"not valid JSONC or unavailable parser: {error}")]
        if not isinstance(data, dict):
            return [_broken_note("opencode", path, detail="top level is not an object")]
    else:
        data = {}
    shape_error = _opencode_shape_error(data, gating)
    if shape_error:
        return [_broken_note("opencode", path, detail=shape_error)]
    before = json.loads(json.dumps(data))

    mcp = data.get("mcp")
    mcp = dict(mcp) if isinstance(mcp, dict) else {}
    entries = []
    changed = False
    for name, spec in servers:
        if isinstance(mcp.get(name), dict):
            entries.append(_entry(name, spec, "already-present"))
            continue
        shape = {"type": "local", "command": list(spec["command"]), "enabled": True}
        if dry_run:
            entries.append(_entry(name, spec, "would-install",
                                   diff=json.dumps({name: shape}, indent=2)))
            continue
        mcp[name] = shape
        changed = True
        if spec.get("auth") not in (None, "none"):
            entries.append(_entry(name, spec, "needs-auth", detail=spec.get("authNote", "")))
        else:
            entries.append(_entry(name, spec, "installed"))
    if changed:
        data["mcp"] = mcp

    gate_changed, gate_entries = _apply_opencode_gating(data, gating, dry_run)
    entries.extend(gate_entries)
    changed = changed or gate_changed

    if dry_run or not changed:
        return entries

    if raw:
        text, error = _jsonc_apply_missing(raw, before, data)
        if error:
            return entries + [_broken_note("opencode", path, detail=f"could not edit JSONC: {error}")]
    else:
        text = json.dumps(data, indent=2) + "\n"
    _write_config(path, text)
    return entries


# ---- hermes ------------------------------------------------------------

def _yaml_scalar(value):
    """A JSON-quoted string is a valid YAML flow scalar — the same trick
    memory.py:176-178 uses for frontmatter, and for the same reason: it needs
    no YAML library to be correct."""
    if re.fullmatch(r"[A-Za-z0-9._/@-]+", value):
        return value
    return json.dumps(value)


def _hermes_yaml_block(servers):
    """A copy-paste block, never written to disk (see module note above apply
    and the SAFETY RULES in the brief this implements: no YAML library ships
    with this Python, and the repo carries no third-party deps, so
    config.yaml itself is never touched). This is deliberately the simplest
    possible emitter — it only ever serializes a server name plus a flat list
    of plain-string command args, which is exactly what mcp.servers in
    config/models.json holds, so no general-purpose YAML writer is needed.
    The key name (mcp_servers) is this script's best-effort guess at Hermes'
    schema; there is no Hermes install on the machine this was written on to
    confirm it against, so the surrounding report says so and asks the user
    to adjust it if their version differs.
    """
    lines = ["mcp_servers:"]
    for name, spec in servers:
        command = spec["command"]
        lines.append(f"  {name}:")
        lines.append(f"    command: {_yaml_scalar(command[0])}")
        if len(command) > 1:
            args = ", ".join(_yaml_scalar(a) for a in command[1:])
            lines.append(f"    args: [{args}]")
    return "\n".join(lines) + "\n"


def _apply_hermes(servers, dry_run):
    entries = []
    for index, (name, spec) in enumerate(servers):
        entries.append({
            "kind": "server", "server": name, "label": spec.get("label", name),
            "status": "manual",
            "detail": ("Hermes' config.yaml has no writer here — no YAML library ships "
                       "with this Python and the repo carries no third-party deps — so "
                       "nothing is installed. Paste the block below into "
                       "$HERMES_HOME/config.yaml yourself; the key name is a best-effort "
                       "guess (no Hermes install was available to confirm the schema "
                       "against) — adjust it if your version differs."),
            "command": None,
            "diff": _hermes_yaml_block(servers) if index == 0 else None,
        })
    return entries


_INSTALL = {
    "claude": _apply_claude,
    "codex": _apply_codex,
    "cursor": _apply_cursor,
    "hermes": _apply_hermes,
}


# ---- rendering + dispatch -------------------------------------------------

def _render_unsupported(harness, source):
    checked = list(doctor.HARNESS_ENV) + ["LEOS_AGENT_HARNESS"]
    present = [v for v in checked if os.environ.get(v)]
    absent = [v for v in checked if not os.environ.get(v)]
    lines = [
        "leo setup apply: cannot determine a supported harness — refusing to touch anything.",
        f"  detected: {harness!r} (via {source})",
        f"  env present: {', '.join(present) or '(none)'}",
        f"  env absent:  {', '.join(absent)}",
        "",
        f"  known harnesses: {', '.join(sorted(doctor._known_harnesses()))}",
        "  Pass --harness <name> to state it explicitly.",
    ]
    return "\n".join(lines) + "\n"


def _render_apply(harness, source, dry_run, entries, toggles):
    lines = [
        f"leo setup apply — harness: {harness} (detected via {source})"
        + (" [dry run — nothing executed]" if dry_run else ""),
        "",
        "MCP servers",
    ]
    servers = [e for e in entries if e["kind"] != "gate"]
    gates = [e for e in entries if e["kind"] == "gate"]
    if not servers:
        lines.append("  (no core servers configured for this harness)")
    by_status = {}
    for e in servers:
        by_status.setdefault(e["status"], []).append(e)
    for status in _STATUS_ORDER:
        for e in by_status.get(status, []):
            label = STATUS_LABELS.get(status, status)
            server = e["server"] or "-"
            lines.append(f"  {label:<24} {server:<16} {e['label']}")
            if e.get("command"):
                lines.append(f"    $ {shlex.join(e['command'])}")
            if e.get("detail"):
                lines.extend(f"    {line}" for line in e["detail"].splitlines())
            if e.get("diff"):
                lines.append("    --- would write ---" if dry_run else "    --- paste this ---")
                lines.extend(f"    {line}" for line in e["diff"].splitlines())

    if gates:
        lines.append("")
        lines.append("OpenCode tool gating")
        by_status = {}
        for e in gates:
            by_status.setdefault(e["status"], []).append(e)
        for status in _STATUS_ORDER:
            for e in by_status.get(status, []):
                lines.append(f"  {STATUS_LABELS.get(status, status):<24} {e['label']}")

    if toggles:
        lines.append("")
        lines.append("Harness toggles (report only — apply never flips these for you)")
        for t in toggles:
            lines.append(f"  {t['name']}: {t['status']}")
            if t.get("detail"):
                lines.extend(f"    {line}" for line in t["detail"].splitlines())

    lines.append("")
    return "\n".join(lines)


def cmd_apply(argv):
    dry_run = "--dry-run" in argv
    detect_argv = [a for a in argv if a != "--dry-run"]
    harness, source = doctor._detect_harness(detect_argv)
    known = doctor._known_harnesses()
    if harness == "unknown" or harness not in known:
        print(_render_unsupported(harness, source))
        return 1

    mcp = _catalogue_or_error()
    if mcp is None:
        return 1
    servers = _core_servers(mcp, harness)
    if harness == "opencode":
        gating = ((mcp.get("gating") or {}).get("opencode")) or {}
        entries = _apply_opencode(servers, gating, dry_run)
    else:
        installer = _INSTALL.get(harness)
        if installer is None:
            print(f"leo setup apply: {harness!r} has no installer wired up yet.")
            return 1
        entries = installer(servers, dry_run)
    toggles = _TOGGLES.get(harness, lambda: [])()
    print(_render_apply(harness, source, dry_run, entries, toggles))
    # A refused write (malformed config) or an install command that ran and
    # returned non-zero is a failure even though the run reported it politely —
    # the docstring promises 1 on a failed write, and a caller chaining on `&&`
    # has to be able to see it. `manual` is not a failure: Hermes, the
    # missing-binary case, and Claude-in-Chrome all reach it having correctly
    # done nothing.
    if any(entry.get("status") in ("error", "failed") for entry in entries):
        return 1
    return 0


# --------------------------------------------------------------------------
# connectors / connect: vendor MCP servers, opt-in and never auto-offered
# --------------------------------------------------------------------------
#
# `apply` bootstraps the CORE servers every harness gets. Vendor connectors
# (Slack, Sentry, Linear, ...) are a second, deliberately separate list in
# the same `mcp` config — `mcp.connectors` — that `apply` never touches.
# `connectors`/`connectors --json` are READ-ONLY: they report which of those
# are already registered against the running harness and which are not, so
# an agent-side skill can drive a multi-select from the JSON and this script
# never has to guess what the user wants. `connect <key>...` is the only
# thing here that writes, and only for the keys named explicitly.

def _connector_catalogue():
    return {c["key"]: c for c in (_mcp_config().get("connectors") or []) if c.get("key")}


def _index_url_servers(servers):
    """From a harness's own {name: entry} MCP server map, everything needed
    to match a connector: every registered name, and a url -> name map for
    entries that carry a "url" key (every connector's shape, on every
    harness that writes JSON directly — see _connect_cursor/_connect_opencode
    below). A server with no "url" (a local/stdio entry) still counts for the
    name-match fallback."""
    names, by_url = set(), {}
    for name, entry in servers.items():
        names.add(name)
        if isinstance(entry, dict) and entry.get("url"):
            by_url[entry["url"]] = name
    return names, by_url


def _parse_claude_mcp_list_line(line):
    """`<name>: <url-or-command> - <status>`, e.g.
    'claude.ai Granola: https://mcp.granola.ai/mcp - ✔ Connected' or
    'context7: npx -y @upstash/context7-mcp - ✔ Connected'. Header and
    blank lines carry neither ': ' nor ' - ' and are skipped rather than
    mis-parsed.

    The value is reduced to its first whitespace-delimited token rather than
    kept whole. This format is not a contract, and every plausible drift —
    an extra ` - 12 tools`, a ` (HTTP)` marker — otherwise survives the
    rsplit and lands in the URL map under a key nothing will ever match. That
    failure is silent and worse than useless: an installed connector reads as
    available and `connect` registers it twice.
    """
    left, sep, rest = line.strip().partition(": ")
    if not sep or not left:
        return None
    parts = rest.rsplit(" - ", 1)
    if len(parts) != 2 or not parts[0].strip():
        return None
    value = parts[0].strip().split()[0]
    return left, value


def _claude_registered(gate):
    """Every name/URL this harness's own view of MCP servers already
    carries: the `mcpServers` block `~/.claude.json` holds at user scope
    (what apply's core install and this file's own `--scope user` connect
    both write), PLUS whatever `claude mcp list` reports live. The second
    read is what actually matters for the connector menu — a claude.ai
    connector such as Gmail or Vercel is registered against the account,
    never written into `~/.claude.json` at all, and `claude mcp list` is the
    only place its URL is visible (confirmed against this machine's own
    Claude Code install).

    Returns (names, by_url, available). `available` is False when the live
    read did not happen, because "I looked and found nothing" and "I could
    not look" must not render the same: the second one offering all eleven
    connectors would have the user re-register what they already have.
    """
    names, by_url = set(), {}
    if not os.path.isdir(gate):
        return names, by_url, False
    data = _read_json_file(_claude_json_path()) or {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    servers = servers if isinstance(servers, dict) else {}
    inner_names, inner_by_url = _index_url_servers(servers)
    names |= inner_names
    by_url.update(inner_by_url)
    try:
        result = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        result = None
    available = result is not None and result.returncode == 0
    if available:
        parsed_any = False
        for line in result.stdout.splitlines():
            parsed = _parse_claude_mcp_list_line(line)
            if parsed is None:
                continue
            parsed_any = True
            name, value = parsed
            names.add(name)
            if value.startswith("http://") or value.startswith("https://"):
                by_url[value] = name
        # Exited clean, said something, and none of it parsed: the output
        # format moved. Degraded, not empty — see the docstring.
        if result.stdout.strip() and not parsed_any:
            available = False
    return names, by_url, available


def _codex_registered(gate):
    """`codex mcp list --json` is the one read that carries the actual
    registered URL for a streamable-HTTP server (confirmed against
    codex-cli 0.145.0: `transport.type == "streamable_http"`,
    `transport.url` holds the endpoint). Parsing config.toml directly would
    need tomllib, 3.11+, not guaranteed on this file's Python floor; `codex
    mcp get <name>` only proves presence under a name already known, not the
    URL a connector may have been registered under a different one with.

    Returns (names, by_url, available); see _claude_registered on why a read
    that could not happen must not look like a read that found nothing.
    """
    names, by_url = set(), {}
    if not os.path.isdir(gate):
        return names, by_url, False
    try:
        result = subprocess.run(["codex", "mcp", "list", "--json"],
                                 capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return names, by_url, False
    if result.returncode != 0:
        return names, by_url, False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return names, by_url, False
    if not isinstance(data, list):
        return names, by_url, False
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        names.add(name)
        url = (entry.get("transport") or {}).get("url")
        if url:
            by_url[url] = name
    return names, by_url, True


def _read_cursor_existing():
    gate = _harness_dir("cursor")
    if not os.path.isdir(gate):
        return {}
    path = os.path.join(gate, "mcp.json")
    data = _read_json_file(path)
    if data is None and os.path.exists(path):
        return None
    existing = data.get("mcpServers") if isinstance(data, dict) else None
    return existing if isinstance(existing, dict) else {}


def _read_opencode_mcp():
    gate = _harness_dir("opencode")
    if not os.path.isdir(gate):
        return {}
    try:
        path = _opencode_config_path(gate)
    except ValueError:
        return None
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        data, error = _jsonc_parse(raw)
        if error:
            return None
    except OSError:
        return None
    if not isinstance(data, dict) or _opencode_shape_error(data):
        return None
    return data.get("mcp") or {}


def _hermes_registered():
    """Read Hermes' own registry; an unfamiliar format is unknown, not empty."""
    try:
        result = subprocess.run(["hermes", "mcp", "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set(), {}, False
    if result.returncode:
        return set(), {}, False
    output = result.stdout.strip()
    if not output or re.search(r"\b(no|none|0)\b.*\b(mcp|server)", output, re.I):
        return set(), {}, True
    known = set(_connector_catalogue())
    names = set()
    for line in output.splitlines():
        match = re.match(r"\s*([A-Za-z0-9_.-]+)(?:\s|:)", line)
        if match and match.group(1) in known:
            names.add(match.group(1))
    return (names, {}, True) if names else (set(), {}, False)


_REGISTERED = {
    "claude": lambda: _claude_registered(_harness_dir("claude")),
    "codex": lambda: _codex_registered(_harness_dir("codex")),
    # The file-backed harnesses read their own config directly, so detection
    # is always available; hermes has no YAML reader, so it is never available
    # and the report must say so rather than imply an empty machine.
    "cursor": lambda: (_index_url_servers(_read_cursor_existing()) + (True,)
                       if _read_cursor_existing() is not None else (set(), {}, False)),
    "opencode": lambda: (_index_url_servers(_read_opencode_mcp()) + (True,)
                         if _read_opencode_mcp() is not None else (set(), {}, False)),
    "hermes": _hermes_registered,
}


def _normalize_endpoint(url):
    """Compare endpoints the way a server would, not the way a string does.

    The URL map exists to catch a connector registered under a name of the
    user's choosing. An exact string compare defeats that on the differences
    every URL picks up in the wild — a trailing slash, a capitalized host —
    and a false negative here is not a cosmetic miss: `connect` goes on to
    register the same endpoint a second time under its own key.
    """
    text = (url or "").strip()
    if not text:
        return ""
    scheme, sep, rest = text.partition("://")
    if not sep:
        return text.rstrip("/").lower()
    host, slash, path = rest.partition("/")
    normalized = scheme.lower() + "://" + host.lower()
    if slash:
        normalized += "/" + path
    return normalized.rstrip("/")


def _already_registered(key, url, names, by_url):
    """The single already-present test, shared by the report and every
    connect path. They must agree: a report that says `installed` while
    connect writes it anyway is how one endpoint ends up registered twice.
    """
    target = _normalize_endpoint(url)
    if target and target in {_normalize_endpoint(k) for k in by_url}:
        return True
    return key in names


def _connector_installed(connector, names, by_url):
    return _already_registered(connector["key"], connector.get("url"), names, by_url)


def _render_connectors_unsupported(harness, source):
    return (
        f"leo setup connectors: cannot determine a supported harness (detected "
        f"{harness!r} via {source}) — nothing installable to report.\n"
        "  Pass --harness <name> to state it explicitly.\n"
    )


def _render_connectors(harness, source, items, detection_available=True):
    lines = [f"leo setup connectors — harness: {harness} (detected via {source})", ""]
    installed = [c for c in items if c["installed"]]
    available = [c for c in items if not c["installed"]]
    if not detection_available:
        # Every other blind spot in this file is disclosed in its own output;
        # this one has to be too, or an empty Installed list reads as fact.
        lines.append("  NOTE: could not read this harness's registered servers, so nothing")
        lines.append("  below is known to be installed — the list may offer what you already")
        lines.append("  have. Check the harness yourself before installing.")
        lines.append("")
    lines.append("Installed")
    for c in installed or [None]:
        lines.append("  (none)" if c is None else f"  {c['key']:<14} {c['label']}")
    lines.append("")
    lines.append("Available (not installed)")
    for c in available or [None]:
        if c is None:
            lines.append("  (none)")
            continue
        note = "  [needs --url — the endpoint cannot be guessed]" if c["needsUrl"] else ""
        lines.append(f"  {c['key']:<14} {c['label']}{note}")
        if c["authNote"]:
            lines.append(f"    {c['authNote']}")
    lines.append("")
    lines.append("Install with: setup.py connect <key> [<key> ...]")
    lines.append("(snowflake also needs: setup.py connect snowflake --url <URL>)")
    return "\n".join(lines) + "\n"


def cmd_connectors(argv):
    argv = list(argv)
    json_out = "--json" in argv
    harness, source = doctor._detect_harness(argv)
    known = doctor._known_harnesses()
    mcp = _catalogue_or_error()
    if mcp is None:
        return 1
    connectors = [c for c in (mcp.get("connectors") or [])]

    if harness == "unknown" or harness not in known:
        if json_out:
            print(json.dumps({"harness": harness, "source": source, "supported": False,
                               "connectors": []}, indent=2, sort_keys=True))
        else:
            print(_render_connectors_unsupported(harness, source))
        return 0

    registered = _REGISTERED.get(harness, lambda: (set(), {}, False))
    names, by_url, detection_available = registered()
    items = []
    for c in connectors:
        url = c.get("url") or ""
        present = _connector_installed(c, names, by_url)
        # A successful local/config read can prove presence even when a
        # supplemental live CLI query failed; it can never prove absence.
        presence = "present" if present else ("absent" if detection_available else "unknown")
        items.append({
            "key": c["key"],
            "label": c.get("label", c["key"]),
            "url": url,
            "transport": c.get("transport", ""),
            "auth": c.get("auth", ""),
            "authNote": c.get("authNote", ""),
            "registration": c.get("registration", ""),
            "prerequisites": c.get("prerequisites", []),
            "installed": presence == "present",  # compatibility for callers
            "presence": presence,
            "needsUrl": not bool(url),
        })

    if json_out:
        print(json.dumps({"harness": harness, "source": source, "supported": True,
                           "detectionAvailable": detection_available,
                           "connectors": items}, indent=2, sort_keys=True))
    else:
        print(_render_connectors(harness, source, items, detection_available))
    return 0


# ---- connect: the one action in this section ------------------------------

_CODEX_CONNECTOR_TIMEOUT = 12  # seconds


def _shell_install_codex_connector(name, spec, cmd):
    """`codex mcp add <name> --url <url>` writes the entry to config.toml
    immediately, then — for every OAuth-capable server, which is all eleven
    connectors — blocks waiting on a local callback for the browser consent
    flow, with no flag to skip it (confirmed against codex-cli 0.145.0:
    "Added global MCP server '<name>'." prints and the entry is already on
    disk before "Starting OAuth flow..." even appears). setup never waits on
    the user's browser — that is the harness's job, on first use — so this
    timeout is short and deliberate. On expiry the process is killed and the
    harness's own config is re-read once to see whether the write landed."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=_CODEX_CONNECTOR_TIMEOUT)
    except subprocess.TimeoutExpired:
        names, _, _ = _codex_registered(_harness_dir("codex"))
        if name in names:
            return _entry(name, spec, "needs-auth", command=cmd, detail=spec.get("authNote", ""))
        return _entry(name, spec, "manual", command=cmd,
                      detail=(f"`{shlex.join(cmd)}` did not finish within "
                              f"{_CODEX_CONNECTOR_TIMEOUT}s and no entry was written; "
                              "run it yourself."))
    except (OSError, subprocess.SubprocessError) as exc:
        return _entry(name, spec, "manual", command=cmd, detail=f"could not run {cmd[0]!r}: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:400]
        return _entry(name, spec, "failed", command=cmd, detail=detail)
    return _entry(name, spec, "needs-auth", command=cmd, detail=spec.get("authNote", ""))


def _connect_claude(resolved):
    gate = _harness_dir("claude")
    if not os.path.isdir(gate):
        return [_no_gate_note("claude", gate)]
    names, by_url, _ = _claude_registered(gate)
    entries = []
    for key, spec in resolved:
        url = spec["url"]
        if _already_registered(key, url, names, by_url):
            entries.append(_entry(key, spec, "already-present"))
            continue
        cmd = ["claude", "mcp", "add", "--transport", "http", "--scope", "user", key, url]
        entries.append(_shell_install(key, spec, cmd))
    return entries


def _connect_codex(resolved):
    gate = _harness_dir("codex")
    if not os.path.isdir(gate):
        return [_no_gate_note("codex", gate)]
    names, by_url, _ = _codex_registered(gate)
    entries = []
    for key, spec in resolved:
        url = spec["url"]
        if _already_registered(key, url, names, by_url):
            entries.append(_entry(key, spec, "already-present"))
            continue
        cmd = ["codex", "mcp", "add", key, "--url", url]
        entries.append(_shell_install_codex_connector(key, spec, cmd))
    return entries


def _connect_cursor(resolved):
    gate = _harness_dir("cursor")
    if not os.path.isdir(gate):
        return [_no_gate_note("cursor", gate)]
    path = os.path.join(gate, "mcp.json")
    data = _read_json_file(path)
    if data is None and os.path.exists(path):
        return [_broken_note("cursor", path)]
    if not isinstance(data, dict):
        data = {}
    existing = data.get("mcpServers")
    existing = dict(existing) if isinstance(existing, dict) else {}
    names, by_url = _index_url_servers(existing)
    entries = []
    changed = False
    for key, spec in resolved:
        url = spec["url"]
        if _already_registered(key, url, names, by_url):
            entries.append(_entry(key, spec, "already-present"))
            continue
        existing[key] = {"url": url}
        changed = True
        entries.append(_entry(key, spec, "needs-auth", detail=spec.get("authNote", "")))
    if changed:
        data["mcpServers"] = existing
        _write_config(path, json.dumps(data, indent=2) + "\n")
    return entries


def _connect_opencode(resolved):
    gate = _harness_dir("opencode")
    if not os.path.isdir(gate):
        return [_no_gate_note("opencode", gate)]
    try:
        path = _opencode_config_path(gate)
    except ValueError as exc:
        return [_broken_note("opencode", gate, detail=str(exc))]
    raw = ""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            return [_broken_note("opencode", path, detail=str(exc))]
        data, error = _jsonc_parse(raw)
        if error:
            return [_broken_note("opencode", path, detail=f"not valid JSONC or unavailable parser: {error}")]
        if not isinstance(data, dict):
            return [_broken_note("opencode", path, detail="top level is not an object")]
    else:
        data = {}
    shape_error = _opencode_shape_error(data)
    if shape_error:
        return [_broken_note("opencode", path, detail=shape_error)]
    before = json.loads(json.dumps(data))

    mcp = data.get("mcp")
    mcp = dict(mcp) if isinstance(mcp, dict) else {}
    names, by_url = _index_url_servers(mcp)
    entries = []
    changed = False
    for key, spec in resolved:
        url = spec["url"]
        if _already_registered(key, url, names, by_url):
            entries.append(_entry(key, spec, "already-present"))
            continue
        mcp[key] = {"type": "remote", "url": url, "enabled": True}
        changed = True
        entries.append(_entry(key, spec, "needs-auth", detail=spec.get("authNote", "")))
    if changed:
        data["mcp"] = mcp

    if not changed:
        return entries

    if raw:
        text, error = _jsonc_apply_missing(raw, before, data)
        if error:
            return entries + [_broken_note("opencode", path, detail=f"could not edit JSONC: {error}")]
    else:
        text = json.dumps(data, indent=2) + "\n"
    _write_config(path, text)
    return entries


def _hermes_connector_yaml_block(resolved):
    lines = ["mcp_servers:"]
    for key, spec in resolved:
        lines.append(f"  {key}:")
        lines.append(f"    url: {_yaml_scalar(spec['url'])}")
    return "\n".join(lines) + "\n"


def _connect_hermes(resolved):
    entries = []
    for key, spec in resolved:
        if spec.get("registration") != "automatic":
            entries.append(_entry(key, spec, "manual", detail=spec.get("authNote", "")))
            continue
        add = ["hermes", "mcp", "add", key, "--url", spec["url"], "--auth", "oauth"]
        added = _shell_install(key, spec, add)
        if added["status"] in ("failed", "manual"):
            entries.append(added)
            continue
        login = ["hermes", "mcp", "login", key]
        try:
            result = subprocess.run(login, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            entries.append(_entry(key, spec, "manual", command=login, detail=str(exc)))
            continue
        if result.returncode:
            entries.append(_entry(key, spec, "failed", command=login,
                                  detail=(result.stderr or result.stdout or "").strip()[:400]))
        else:
            entries.append(_entry(key, spec, "needs-auth", command=login,
                                  detail=spec.get("authNote", "")))
    return entries


_CONNECT = {
    "claude": _connect_claude,
    "codex": _connect_codex,
    "cursor": _connect_cursor,
    "opencode": _connect_opencode,
    "hermes": _connect_hermes,
}


def _render_connect(harness, source, entries):
    lines = [f"leo setup connect — harness: {harness} (detected via {source})", ""]
    by_status = {}
    for e in entries:
        by_status.setdefault(e["status"], []).append(e)
    for status in _STATUS_ORDER:
        for e in by_status.get(status, []):
            label = STATUS_LABELS.get(status, status)
            server = e["server"] or "-"
            lines.append(f"  {label:<24} {server:<16} {e['label']}")
            if e.get("command"):
                lines.append(f"    $ {shlex.join(e['command'])}")
            if e.get("detail"):
                lines.extend(f"    {line}" for line in e["detail"].splitlines())
            if e.get("diff"):
                lines.append("    --- paste this ---")
                lines.extend(f"    {line}" for line in e["diff"].splitlines())
    lines.append("")
    return "\n".join(lines)


def cmd_connect(argv):
    argv = list(argv)
    keys = []
    url_override = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--url" and i + 1 < len(argv):
            url_override = argv[i + 1]
            i += 2
            continue
        if a.startswith("--url="):
            url_override = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--harness" and i + 1 < len(argv):
            i += 2
            continue
        if a.startswith("--harness="):
            i += 1
            continue
        keys.append(a)
        i += 1

    harness, source = doctor._detect_harness(argv)
    known = doctor._known_harnesses()
    if harness == "unknown" or harness not in known:
        print(_render_unsupported(harness, source))
        return 1

    mcp = _catalogue_or_error()
    if mcp is None:
        return 1
    catalogue = {c["key"]: c for c in (mcp.get("connectors") or [])}
    if not keys:
        print(f"setup: connect needs at least one connector key "
              f"(known: {', '.join(sorted(catalogue))})", file=sys.stderr)
        return 1
    unknown_keys = [k for k in keys if k not in catalogue]
    if unknown_keys:
        print(f"setup: unknown connector key(s): {', '.join(unknown_keys)} "
              f"(known: {', '.join(sorted(catalogue))})", file=sys.stderr)
        return 1
    if url_override and len(keys) != 1:
        print("setup: --url applies to exactly one connector key at a time", file=sys.stderr)
        return 1

    resolved = []
    for key in keys:
        spec = dict(catalogue[key])
        if url_override:
            spec["url"] = url_override
        if not spec.get("url"):
            print(f"setup: connect {key}: no default URL on record for this connector — "
                  "its endpoint cannot be guessed. Pass --url <URL> yourself. "
                  f"({spec.get('authNote', '')})", file=sys.stderr)
            return 1
        resolved.append((key, spec))

    manual = [(key, spec) for key, spec in resolved if spec.get("registration") == "manual"]
    manual_entries = [_entry(key, spec, "manual", detail="Prerequisites: " + "; ".join(spec["prerequisites"]))
                      for key, spec in manual]
    automatic = [(key, spec) for key, spec in resolved if spec.get("registration") == "automatic"]
    if not automatic:
        print(_render_connect(harness, source, manual_entries))
        return 0

    # An unreadable registry is neither "present" nor "absent".  Refusing
    # here prevents a blind duplicate registration when a harness CLI changed
    # shape or a hand-edited JSON/JSONC file is malformed.
    names, by_url, detection_available = _REGISTERED.get(harness, lambda: (set(), {}, False))()
    if not detection_available:
        print("leo setup connect: registered-server state is unknown; refusing to write blindly.",
              file=sys.stderr)
        return 1

    installer = _CONNECT.get(harness)
    if installer is None:
        print(f"leo setup connect: {harness!r} has no installer wired up yet.")
        return 1
    entries = manual_entries + installer(automatic)
    print(_render_connect(harness, source, entries))
    if any(e.get("status") in ("error", "failed") for e in entries):
        return 1
    return 0


def main(argv):
    argv = list(argv)
    if argv and argv[0] == "apply":
        return cmd_apply(argv[1:])

    if argv and argv[0] == "connectors":
        return cmd_connectors(argv[1:])

    if argv and argv[0] == "connect":
        return cmd_connect(argv[1:])

    if argv and argv[0] in ("enable", "disable"):
        if len(argv) < 2:
            print(f"setup: {argv[0]} needs a feature name: {', '.join(sorted(FEATURES))}",
                  file=sys.stderr)
            return 1
        feature = argv[1]
        if feature not in FEATURES:
            print(f"setup: unknown feature {feature!r} "
                  f"(known: {', '.join(sorted(FEATURES))})", file=sys.stderr)
            return 1
        want = argv[0] == "enable"
        if _enabled(_read(), feature) == want:
            print(f"{feature} is already {'on' if want else 'off'}; nothing to do")
            return 0
        try:
            _write(feature, want)
        except Exception as exc:
            print(f"setup: could not record the change: {exc}", file=sys.stderr)
            return 1
        print(f"{feature} is now {'on' if want else 'off'}")
        return 0

    data = collect()
    if "--json" in argv:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        sys.stdout.write(_render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
