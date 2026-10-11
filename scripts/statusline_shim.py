#!/usr/bin/env python3
# Managed by leos-agent: the stable launcher Claude Code's statusLine points at.
"""Run the cost status line from whichever leos-agent plugin version is current.

The plugin lives under a versioned cache directory that changes on every
upgrade, so settings.json cannot name statusline.py itself. `leo-install.py
claude --statusline` writes a copy of this file, with the two constants below
filled in, to <data root>/bin/statusline and points statusLine at that. At run
time it picks the plugin root in this order: LEOS_AGENT_ROOT, the newest
installed leos-agent version in Claude's plugin cache, then the root it was
installed from. It runs the script in this process (one interpreter start) and
prints the plainest line on any failure.

The installer replaces the two lines below; keep them exactly as they are.
"""
import os
import runpy
import sys

FALLBACK = ""
CONFIG = ""


def usable(root):
    return bool(root) and os.path.isfile(os.path.join(root, "scripts", "statusline.py"))


def _version(name):
    parts = name.split(".")
    return tuple(int(part) for part in parts) if all(part.isdigit() for part in parts) else ()


def newest(config, environ):
    """The newest cached leos-agent version that carries the script, or None."""
    base = os.path.join(environ.get("CLAUDE_CONFIG_DIR") or config, "plugins", "cache")
    best = None
    try:
        for market in os.listdir(base):
            folder = os.path.join(base, market, "leos-agent")
            try:
                versions = os.listdir(folder)
            except OSError:
                continue
            for name in versions:
                path = os.path.join(folder, name)
                if not usable(path):
                    continue
                try:
                    key = (_version(name), os.path.getmtime(path))
                except OSError:
                    continue
                if best is None or key > best[0]:
                    best = (key, path)
    except OSError:
        return None
    return best[1] if best else None


def resolve(fallback, config, environ=None):
    """The plugin root to run the script from, or None when there is none."""
    environ = os.environ if environ is None else environ
    explicit = environ.get("LEOS_AGENT_ROOT")
    if usable(explicit):
        return explicit
    return newest(config, environ) or (fallback if usable(fallback) else None)


def main():
    try:
        root = resolve(FALLBACK, CONFIG)
        if root is None:
            raise LookupError("no leos-agent plugin root with scripts/statusline.py")
        sys.path.insert(0, os.path.join(root, "scripts"))
        runpy.run_path(os.path.join(root, "scripts", "statusline.py"), run_name="__main__")
    except Exception:
        # A status line has no one to report to.
        print("Claude")


if __name__ == "__main__":
    main()
