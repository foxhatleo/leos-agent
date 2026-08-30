#!/usr/bin/env python3
"""routing: per-machine model routing for leos-agent's economical tier.

The tier only ever had teeth on Claude Code and Codex, because those are the two
harnesses whose model names the payload could hardcode. Every other harness fell
through to "use the current model", so fan-outs there ran at full price. Which
models a harness actually offers varies by machine and by what an IT department
allows, so the mapping cannot ship in the plugin -- it is machine-local config.

CONFIG lives beside the rest of leos-agent's data, at
${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/routing.json, never inside the
plugin: an upgrade, a reinstall, or an uninstall must never take it. Only `set`
and `unset` write it, and only when someone runs them -- the installer reads and
never writes, so an upgrade, a reinstall, or an --uninstall cannot touch it. A
missing file is not an error -- it means "the shipped defaults", which is
exactly the behaviour that predates this file.

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
  routing.py set --harness H --runner M [--runner-effort E]
                              [--executor M] [--executor-effort E]
  routing.py unset --harness H [--runner] [--executor]

Exit codes: 0 ok, 1 on a malformed config or a refused write, 2 on bad usage.
"""
import argparse
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Same data root, and the same locking and atomic-write primitives: both
# machine-local JSON files are written the one way, so a half-written config can
# never survive a crash and two concurrent writers cannot lose an update.
from state import _data_root, _locked, atomic_write  # noqa: E402

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


def read_raw():
    """The document exactly as written, or {} when there is no file.

    set/unset merge into THIS rather than into load()'s output: load()
    normalises a bare model string into an object and fills in effort: None, so
    writing its result back would rewrite every other harness's entry as a side
    effect of touching one. Reading twice is the price of leaving the rest of
    Leo's file exactly as he wrote it.
    """
    try:
        with open(config_path()) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        _bad(f"is not valid JSON ({exc})")
    except OSError as exc:
        _bad(exc.strerror or str(exc))


def load(harnesses=HARNESSES):
    """Parse and validate the config. Returns {} when there is no file."""
    return validate(read_raw(), harnesses)


def validate(data, harnesses=HARNESSES):
    """Resolve a parsed document into {harness: {role: {model, effort}}}.

    Shared by the read path and the write path, so a `set` can never produce a
    file that `load` would go on to reject.
    """
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


def _entry(model, effort):
    """The on-disk shape for one role. No `effort: null` when there is none."""
    entry = {"model": model}
    if effort:
        entry["effort"] = effort
    return entry


def apply_set(data, harness, roles):
    """roles is {role: (model, effort)}; each named role is replaced whole.

    Mutates and returns the document it is given; edit() hands it a copy.

    Wholesale, not merged: a `set` that kept a previously configured effort
    would make it sticky and invisible. The caller prints what it displaced.
    """
    entry = dict(data.get(harness) or {})
    for role, (model, effort) in roles.items():
        entry[role] = _entry(model, effort)
    data[harness] = entry
    return data


def apply_unset(data, harness, roles):
    """roles is a tuple of role names, or () for the whole harness.

    Mutates and returns the document it is given; edit() hands it a copy.
    """
    if harness not in data:
        return data
    if not roles:
        del data[harness]
        return data
    entry = dict(data[harness])
    for role in roles:
        entry.pop(role, None)
    # An empty harness key is a shape load() never returns, so never leave one.
    if entry:
        data[harness] = entry
    else:
        del data[harness]
    return data


def edit(mutate, write=True):
    """Read-modify-write under state's flock. Returns (before, after).

    The lock and the directory creation live only on the write path, so a
    --dry-run and every read subcommand still create nothing in the data root.
    """
    if not write:
        before = _sound()
        return before, mutate(copy.deepcopy(before))
    with _locked(config_path()):
        before = _sound()
        after = mutate(copy.deepcopy(before))
        if after != before:
            # Never write something load() would go on to reject.
            validate(after)
            atomic_write(config_path(), after)
    return before, after


def _sound():
    """The raw document, refused unless it is one a write could safely edit.

    Validating what is already there before touching it means a file Leo broke
    by hand is reported, never repaired by overwriting -- and it keeps the
    mutators free to assume the shape they were written for.
    """
    data = read_raw()
    validate(data)
    return data


def _described(entry):
    """`model` plus `effort=x`, for the column output."""
    if not entry:
        return ""
    effort = f" effort={entry['effort']}" if entry.get("effort") else ""
    return f"{entry['model']}{effort}"


def _resolved(data, harness, role):
    """One role of a raw document, in the normalised shape, or None."""
    value = (data.get(harness) or {}).get(role)
    if isinstance(value, str):
        return {"model": value}
    return value


def _fallback(harness):
    return "shipped default" if harness in BAKED else "inherits the current model"


NOTES = {
    "claude": (
        "note: claude bakes its models into agents/*.md; this layers a per-dispatch\n"
        "      `model:` override on top and never rewrites the plugin."
    ),
    "codex": (
        "note: codex bakes its models into the installed profile TOMLs; this\n"
        "      substitutes them there, and effort lands as model_reasoning_effort."
    ),
}


def _finish(args, before, after, root_hint=True):
    """The trailing lines every write subcommand shares."""
    path = config_path()
    if args.dry_run:
        print(json.dumps(after, indent=1, sort_keys=True))
        print("dry run; nothing written")
        return 0
    if after == before:
        where = f"{path} is already current" if os.path.exists(path) else f"no config at {path}"
        print(f"nothing to write; {where}")
        return 0
    print(f"wrote {path}")
    if root_hint:
        installer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leo-install.py")
        print(f"next: python3 {installer} {args.harness}")
    return 0


def cmd_set(args):
    roles = {}
    for role in ROLES:
        model = getattr(args, role)
        effort = getattr(args, f"{role}_effort")
        if model is None:
            if effort is not None:
                sys.exit(f"routing: --{role}-effort needs --{role} in the same command")
            continue
        if not model.strip():
            _bad(f"{args.harness}.{role}: needs a non-empty 'model'")
        if effort is not None and not effort.strip():
            _bad(f"{args.harness}.{role}.effort: must be a non-empty string when present")
        roles[role] = (model.strip(), effort.strip() if effort else None)
    if not roles:
        sys.exit("routing: set needs --runner and/or --executor")

    before, after = edit(lambda d: apply_set(d, args.harness, roles), write=not args.dry_run)
    for role in ROLES:
        if role not in roles:
            continue
        was = _resolved(before, args.harness, role)
        now = _resolved(after, args.harness, role)
        verb = "unchanged" if was == now else "set"
        suffix = f"  (was {_described(was)})" if was and was != now else ""
        print(f"{verb:9} {args.harness:9} {role:9} {_described(now)}{suffix}")
    if args.harness in NOTES and after != before:
        print(NOTES[args.harness])
    return _finish(args, before, after)


def cmd_unset(args):
    roles = tuple(role for role in ROLES if getattr(args, role))
    before, after = edit(lambda d: apply_unset(d, args.harness, roles), write=not args.dry_run)
    touched = roles or ROLES
    for role in touched:
        was = _resolved(before, args.harness, role)
        if was:
            print(f"{'unset':9} {args.harness:9} {role:9} {_described(was)} -> {_fallback(args.harness)}")
    if before == after:
        print(f"{'unchanged':9} {args.harness:9} {'(both)':9} nothing configured")
    return _finish(args, before, after)


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

    setter = sub.add_parser("set", help="point one harness's roles at models on this machine")
    setter.add_argument("--harness", choices=HARNESSES, required=True)
    setter.add_argument("--runner", metavar="MODEL", help="replaces the runner entry whole")
    setter.add_argument("--runner-effort", metavar="E", help="needs --runner; omitting it clears any effort")
    setter.add_argument("--executor", metavar="MODEL", help="replaces the executor entry whole")
    setter.add_argument("--executor-effort", metavar="E", help="needs --executor; omitting it clears any effort")
    setter.add_argument("--dry-run", action="store_true", help="show the result, write nothing")

    unsetter = sub.add_parser("unset", help="drop a harness's roles, back to its shipped default")
    unsetter.add_argument("--harness", choices=HARNESSES, required=True)
    unsetter.add_argument("--runner", action="store_true")
    unsetter.add_argument("--executor", action="store_true")
    unsetter.add_argument("--dry-run", action="store_true", help="show the result, write nothing")

    args = parser.parse_args(argv)
    # An explicit branch per mode: a bare else would silently route a new
    # subcommand into render and crash on an attribute it does not have.
    if args.mode == "path":
        print(config_path())
    elif args.mode == "show":
        cmd_show(args)
    elif args.mode == "render":
        print(stanza(args.harness, load(HARNESSES)))
    elif args.mode == "set":
        return cmd_set(args)
    elif args.mode == "unset":
        return cmd_unset(args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
