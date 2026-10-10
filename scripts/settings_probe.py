#!/usr/bin/env python3
"""Key-scoped, read-only probes of harness settings for diagnostics.

Each probe reads only the keys it names. `env` blocks are read one variable at
a time, hook commands are classified rather than echoed, and the only strings
returned are model names and matchers, sanitised and truncated, so a token kept
in a settings file cannot reach a report. Nothing here writes, creates a
directory, or uses the network.
"""
import glob
import json
import os
import re
import shlex
import sys

# Claude Code reads on/off variables as 1/true/yes/on in any casing.
TRUE_WORDS = frozenset(("1", "true", "yes", "on"))
MANAGED_DIRS = ("/Library/Application Support/ClaudeCode",) if sys.platform == "darwin" else ("/etc/claude-code",)
DISPATCH_TOOLS = ("Agent", "Task")
# Feature-flag fetching gates the advisor. The first two disable it when set to
# any non-empty value, the rest only when on; third-party providers lack it.
FLAG_FETCH_ANY_VALUE = ("DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")
FLAG_FETCH_WHEN_ON = ("DISABLE_GROWTHBOOK", "DO_NOT_TRACK")
THIRD_PARTY_PROVIDERS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                         "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_MANTLE")


def truthy(value):
    return isinstance(value, str) and value.strip().lower() in TRUE_WORDS


def clean(value, limit=100):
    """Printable characters only, bounded: settings text is data, not output."""
    return "".join(ch for ch in str(value) if ch.isprintable())[:limit]


def config_dir(harness, env=None, home=None):
    """The installer's config_dir defaults and overrides, without creating anything.
    An empty override counts as unset."""
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    defaults = {"claude": os.path.join(home, ".claude"), "codex": os.path.join(home, ".codex"),
                "cursor": os.path.join(home, ".cursor"), "hermes": os.path.join(home, ".hermes"),
                "pi": os.path.join(home, ".pi", "agent"), "opencode": os.path.join(xdg, "opencode")}
    variables = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "hermes": "HERMES_HOME",
                 "pi": "PI_CODING_AGENT_DIR", "opencode": "OPENCODE_CONFIG_DIR"}
    value = env.get(variables.get(harness, ""))
    return os.path.expanduser(value) if value else defaults[harness]


def project_roots(start=None):
    """The working directory and, when different, its enclosing Git root."""
    start = os.path.abspath(start or os.getcwd())
    roots, here = [start], start
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            if here != start:
                roots.append(here)
            return roots
        parent = os.path.dirname(here)
        if parent == here:
            return roots
        here = parent


def read_json(path):
    """(object, problem). A missing file is (None, None), never an error."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError:
        return None, "unreadable"
    except ValueError:
        return None, "invalid JSON"
    return (data, None) if isinstance(data, dict) else (None, "not a JSON object")


def claude_settings_files(project=None, env=None, home=None, managed_dirs=None):
    """(scope, path) for every settings file Claude Code may read, highest
    precedence first. Server-managed, MDM and --settings sources are not files
    here and stay outside these probes."""
    files = []
    for directory in MANAGED_DIRS if managed_dirs is None else managed_dirs:
        # managed-settings.json merges first and drop-ins after it, in name
        # order, so the last drop-in wins.
        dropins = sorted(p for p in glob.glob(os.path.join(directory, "managed-settings.d", "*.json"))
                         if not os.path.basename(p).startswith("."))
        files += [("managed", p) for p in reversed(dropins)]
        files.append(("managed", os.path.join(directory, "managed-settings.json")))
    user = os.path.join(config_dir("claude", env, home), "settings.json")
    roots = project_roots(project)
    for scope, name in (("local", "settings.local.json"), ("project", "settings.json")):
        for root in roots:
            path = os.path.join(root, ".claude", name)
            if os.path.realpath(path) != os.path.realpath(user):
                files.append((scope, path))
    files.append(("user", user))
    return files


def load_settings(files):
    """[(scope, path, data)] for the readable files, plus their problems."""
    loaded, problems = [], []
    for scope, path in files:
        data, problem = read_json(path)
        if problem:
            problems.append({"scope": scope, "path": path, "problem": problem})
        elif data is not None:
            loaded.append((scope, path, data))
    return loaded, problems


def setting(name, loaded, environ=None, env_key=True):
    """One `env` variable (or, with env_key=False, one top-level key), read
    key-scoped: where it is set and the value that applies. A settings `env`
    entry replaces an inherited shell value, so the highest-precedence file
    wins and the process environment comes last."""
    sources, value = [], None
    for scope, path, data in loaded:
        holder = data.get("env") if env_key else data
        if isinstance(holder, dict) and name in holder and holder[name] is not None:
            sources.append({"scope": scope, "path": path})
            if value is None:
                value = str(holder[name]) if env_key else holder[name]
    environ = os.environ if environ is None else environ
    if env_key and environ.get(name) is not None:
        sources.append({"scope": "environment"})
        if value is None:
            value = environ[name]
    return {"value": value, "set_in": sources}


def matcher_covers(matcher, tool):
    """Claude Code's matcher rules: catch-all, exact names split on | or ,,
    otherwise an unanchored regular expression. None when the pattern is not
    one Python can evaluate."""
    if matcher in (None, "", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    if re.fullmatch(r"[A-Za-z0-9_\- ,|]+", matcher):
        return tool in {part.strip() for part in re.split(r"[|,]", matcher)}
    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return None


def hook_handlers(hooks, event):
    """(matcher, command) for each command handler of `event`, in the nested
    Claude/Codex shape or Cursor's flat one."""
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks")
        if isinstance(handlers, list):
            for handler in handlers:
                if isinstance(handler, dict) and isinstance(handler.get("command"), str):
                    yield group.get("matcher"), handler["command"]
        elif isinstance(group.get("command"), str):
            yield group.get("matcher"), group["command"]


def is_rtk(command):
    """rtk's own hook command (`rtk hook <harness>`) or its legacy script."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    return any(os.path.basename(word) in ("rtk", "rtk.exe") or "rtk-rewrite" in word or "rtk-hook" in word
               for word in words)


def _claude_hook_sources(loaded, config):
    for scope, path, data in loaded:
        yield {"scope": scope, "path": path}, data.get("hooks")
    registry, _ = read_json(os.path.join(config, "plugins", "installed_plugins.json"))
    plugins = (registry or {}).get("plugins")
    enabled = {}
    for _, _, data in reversed(loaded):  # lowest precedence first, so higher scopes overwrite
        value = data.get("enabledPlugins")
        if isinstance(value, dict):
            enabled.update({k: v for k, v in value.items() if isinstance(v, bool)})
    for plugin_id, installs in sorted(plugins.items()) if isinstance(plugins, dict) else []:
        for install in installs if isinstance(installs, list) else [installs]:
            root = install.get("installPath") if isinstance(install, dict) else None
            if not isinstance(root, str):
                continue
            hooks_file = os.path.join(root, "hooks", "hooks.json")
            data, _ = read_json(hooks_file)
            yield ({"scope": "plugin", "plugin": clean(plugin_id), "path": hooks_file,
                    "enabled": enabled.get(plugin_id, "not set")}, (data or {}).get("hooks"))


INTERACTION = {
    "claude": ("rtk rewrites Bash commands only: Read, Grep and Glob bypass it, and leos-agent's python3 helpers "
               "are not rewritten; the git and gh they run as subprocesses never pass a hook. Its hook acts only on "
               "input carrying a command string, which Agent/Task dispatch input lacks, so dispatches reach the "
               "guard unchanged. It would turn attach-pr's stubbed `gh pr create` into the real one, so the "
               "attach-pr resolver adds rtk's RTK_DISABLED=1 opt-out while rtk is configured. Bash output in "
               "transcripts is rtk's compressed form."),
    "codex": ("rtk's Codex hook acts only on Bash calls and needs /hooks trust like any other; spawn_agent is "
              "untouched. Shell output in rollouts is rtk's compressed form."),
    "cursor": ("rtk rewrites shell commands on preToolUse; leos-agent's Cursor check runs on subagentStart, so "
               "the two never act on the same event."),
    "other": "rtk rewrites shell commands before they run; it is not a dispatch hook, and shell output is compressed.",
}


def output_compressors(harness, env=None, home=None, project=None, managed_dirs=None):
    """Bash-rewriting output compressors (rtk) configured on disk for one
    harness. Configuration is not activation: hooks bind at session start."""
    found, other = [], 0
    roots = project_roots(project)
    config = config_dir(harness, env, home)
    if harness == "claude":
        loaded, _ = load_settings(claude_settings_files(project, env, home, managed_dirs))
        for source, hooks in _claude_hook_sources(loaded, config):
            for matcher, command in hook_handlers(hooks, "PreToolUse"):
                if is_rtk(command):
                    covers = [matcher_covers(matcher, tool) for tool in DISPATCH_TOOLS]
                    found.append(dict(source, tool="rtk", matcher=clean(matcher, 60) if matcher is not None else None,
                                      rewrites_bash=matcher_covers(matcher, "Bash") is not False,
                                      receives_dispatch_calls=True if any(covers) else (None if None in covers else False)))
                elif matcher_covers(matcher, "Bash") is not False:
                    other += 1
    elif harness in ("codex", "cursor"):
        event = "PreToolUse" if harness == "codex" else "preToolUse"
        folder = ".codex" if harness == "codex" else ".cursor"
        paths = [("user", os.path.join(config, "hooks.json"))] + [("project", os.path.join(r, folder, "hooks.json")) for r in roots]
        for scope, path in paths:
            data, _ = read_json(path)
            for matcher, command in hook_handlers((data or {}).get("hooks"), event):
                if is_rtk(command):
                    found.append({"scope": scope, "path": path, "tool": "rtk"})
                elif harness == "codex" and matcher_covers(matcher, "Bash") is not False:
                    other += 1
    else:
        candidates = {
            "opencode": [os.path.join(config, d, "rtk.ts") for d in ("plugins", "plugin")],
            "pi": [os.path.join(config, "extensions", "rtk.ts")] + [os.path.join(r, ".pi", "extensions", "rtk.ts") for r in roots],
            "hermes": [os.path.join(config, "plugins", "rtk-rewrite")],
        }.get(harness, [])
        found = [{"path": path, "tool": "rtk"} for path in candidates if os.path.exists(path)]
    result = {"configured": bool(found), "found": found}
    if found:
        result["interaction"] = INTERACTION.get(harness, INTERACTION["other"])
    if other:
        result["other_bash_pretooluse_hooks"] = other
    return result
