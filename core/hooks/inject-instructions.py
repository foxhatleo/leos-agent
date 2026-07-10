#!/usr/bin/env python3
"""SessionStart injector — deliver leos-agent's global instructions to Codex additively.

Codex has one global instruction slot (`~/.codex/AGENTS.md`) and no `@import`; a whole-file symlink
there would clobber the user's own global instructions. Instead a Codex `SessionStart` hook runs
this script, which emits `<clone>/global/AGENTS.md` as `additionalContext` — read live on every
session, so `git pull` upgrades it, and the user's own `~/.codex/AGENTS.md` still loads on top
(verified additive; base prompt preserved).

Self-locates the clone via realpath(__file__) — the same idiom as bash-guard.py / council.py — so it
works through the symlink from any tool home. Fail-open: any error exits 0 with no output, never
breaking session start. `LEOS_GLOBAL_INSTRUCTIONS` overrides the path (used by tests).
"""

import json
import os
import sys


def _global_path():
    here = os.path.realpath(__file__)                       # <clone>/core/hooks/inject-instructions.py
    clone = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    return os.environ.get("LEOS_GLOBAL_INSTRUCTIONS",
                          os.path.join(clone, "global", "AGENTS.md"))


def main():
    try:
        with open(_global_path(), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 0  # fail-open: no global file -> inject nothing, never break session start
    json.dump({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": text}}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
