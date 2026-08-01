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

Exit code is 0 on success, 1 on an unknown feature or a failed write.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import memory  # noqa: E402  (path fix must precede the import)
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


def main(argv):
    argv = list(argv)
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
