#!/usr/bin/env python3
"""routing: per-machine model routing for leos-agent's economical tier.

The tier only ever had teeth on Claude Code and Codex, because those are the two
harnesses whose model names the payload could hardcode. Every other harness fell
through to "use the current model", so fan-outs there ran at full price. Which
models a harness actually offers varies by machine and by what an IT department
allows, so the mapping cannot ship in the plugin -- it is machine-local config.

CONFIG lives beside the rest of leos-agent's data, at
${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/routing.json, never inside the
plugin: an upgrade, a reinstall, or an uninstall must never take it. Nothing
here writes it. A missing file is not an error -- it means "the shipped
defaults", which is exactly the behaviour that predates this file.

  {"cursor":   {"runner": "grok-code-fast-1", "executor": "claude-sonnet-4.6"},
   "opencode": {"runner": "anthropic/claude-haiku-4-5"},
   "codex":    {"runner": {"model": "gpt-5.6-luna", "effort": "low"}}}

Keys are harness names; each holds "runner" and/or "executor", independently. A
bare string is shorthand for {"model": ...}. Model strings are free-form and
never checked against a known-model list -- whatever the harness accepts goes in
verbatim. Only the keys are validated, and an unknown one is a hard error: a
typo that silently left a harness on the expensive model is the one failure this
file exists to prevent.

READ AT INSTALL TIME, NOT AT RUN TIME. leo-install.py renders the result into
the payload block it already writes, so a session pays nothing to know its own
routing -- no config read, no extra turn, no bytes beyond the dispatch line the
payload was always going to carry.

  routing.py show [--harness H]   what is configured, resolved
  routing.py render --harness H   the exact stanza the installer would inject
  routing.py path                 the config file's path

Exit codes: 0 ok, non-zero on a malformed config.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from state import _data_root  # noqa: E402  same data root, deliberately shared

# The canonical harness list. leo-install.py imports it from here rather than
# the other way round: it imports this module to render, and its own filename is
# not an importable one.
HARNESSES = ("claude", "codex", "cursor", "hermes", "pi", "opencode")

CONFIG_NAME = "routing.json"
ROLES = ("runner", "executor")
FIELDS = ("model", "effort")

# Harnesses whose economical tier ships with models already baked in: Claude
# Code reads agents/*.md, Codex reads the installed profile TOMLs. Everything
# else inherits unless this file says otherwise.
BAKED = ("claude", "codex")


def config_path():
    return os.path.join(_data_root(), CONFIG_NAME)


def _bad(message):
    sys.exit(f"routing: {config_path()}: {message}")


def load(harnesses=HARNESSES):
    """Parse and validate the config. Returns {} when there is no file."""
    path = config_path()
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        _bad(f"is not valid JSON ({exc})")
    except OSError as exc:
        _bad(exc.strerror or str(exc))

    if not isinstance(data, dict):
        _bad(f"top level is {type(data).__name__}, expected an object keyed by harness")

    out = {}
    for harness, entry in data.items():
        if harness not in harnesses:
            _bad(f"{harness!r} is not a harness; expected one of {', '.join(sorted(harnesses))}")
        if not isinstance(entry, dict):
            _bad(f"{harness}: expected an object with 'runner' and/or 'executor'")
        roles = {}
        for role, value in entry.items():
            if role not in ROLES:
                _bad(f"{harness}.{role}: unknown key; expected {' or '.join(ROLES)}")
            if isinstance(value, str):
                value = {"model": value}
            if not isinstance(value, dict):
                _bad(f"{harness}.{role}: expected a model name or an object, got {type(value).__name__}")
            for field in value:
                if field not in FIELDS:
                    _bad(f"{harness}.{role}.{field}: unknown field; expected {' or '.join(FIELDS)}")
            model = value.get("model")
            if not isinstance(model, str) or not model.strip():
                _bad(f"{harness}.{role}: needs a non-empty 'model'")
            effort = value.get("effort")
            if effort is not None and (not isinstance(effort, str) or not effort.strip()):
                _bad(f"{harness}.{role}.effort: must be a non-empty string when present")
            roles[role] = {"model": model.strip(), "effort": effort.strip() if effort else None}
        if roles:
            out[harness] = roles
    return out


def profile(config, harness, role):
    """The configured {model, effort} for one role, or None."""
    return (config.get(harness) or {}).get(role)


def _named(entry):
    """`model` or `model`/effort, for prose."""
    if entry.get("effort"):
        return f"`{entry['model']}`/{entry['effort']}"
    return f"`{entry['model']}`"


def stanza(harness, config):
    """The dispatch lines for one harness. No trailing newline.

    Kept deliberately short: this text is always-loaded on every turn of every
    session, so it must cost less than the multi-harness prose it replaces.
    """
    runner = profile(config, harness, "runner")
    executor = profile(config, harness, "executor")

    if harness == "claude":
        # The Agent tool's model parameter overrides the agent definition's
        # frontmatter, so a machine can retarget the tier without the installer
        # ever writing into the plugin-owned agents/ directory.
        if not runner and not executor:
            return 'On Claude Code pass `subagent_type: "leo-runner"` or `"leo-executor"`;\nthe agent definitions carry the models.'
        overrides = ", and ".join(
            f'`subagent_type: "leo-{role}"` with `model: "{entry["model"]}"`'
            for role, entry in (("runner", runner), ("executor", executor))
            if entry
        )
        kept = "" if (runner and executor) else "\nThe other profile keeps the model its agent definition carries."
        return "On Claude Code pass " + overrides + "." + kept

    if harness == "codex":
        # Config reaches Codex through the installed profile TOMLs, so the prose
        # is the same either way -- and stays one line.
        return "On Codex the installed `leo-runner` and `leo-executor` profiles carry\nthe models."

    if not runner and not executor:
        return "No cheaper profile is configured here: use the current model, and say\nrouting could not be applied."

    if runner and executor:
        assignment = f"Dispatch leo-runner at {_named(runner)} and leo-executor at {_named(executor)}."
    elif runner:
        assignment = f"Dispatch leo-runner at {_named(runner)}; leo-executor inherits."
    else:
        assignment = f"Dispatch leo-executor at {_named(executor)}; leo-runner inherits."
    return assignment + "\nWhere this harness cannot set a model per spawn, inherit and say so."


def cmd_show(args):
    config = load()
    if not config:
        print(f"no routing config at {config_path()}; every harness uses its shipped default")
        return
    for harness in sorted(config):
        if args.harness and harness != args.harness:
            continue
        for role in ROLES:
            entry = config[harness].get(role)
            if entry:
                effort = f"  effort={entry['effort']}" if entry["effort"] else ""
                print(f"{harness:9} {role:9} {entry['model']}{effort}")
    for harness in sorted(set(BAKED) - set(config)):
        if not args.harness or harness == args.harness:
            print(f"{harness:9} {'(both)':9} shipped default")


def main(argv):
    parser = argparse.ArgumentParser(prog="routing.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    show = sub.add_parser("show", help="what is configured, resolved")
    show.add_argument("--harness", choices=HARNESSES)
    render = sub.add_parser("render", help="the stanza the installer would inject")
    render.add_argument("--harness", choices=HARNESSES, required=True)
    sub.add_parser("path", help="the config file's path")

    args = parser.parse_args(argv)
    if args.mode == "path":
        print(config_path())
    elif args.mode == "show":
        cmd_show(args)
    else:
        print(stanza(args.harness, load(HARNESSES)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
