#!/usr/bin/env python3
"""SessionStart hook: inject the using-leo policy skill as session context.

A hook failure here would otherwise break every session start (startup,
/clear, /compact) for a policy-injection convenience — that trade is never
worth it. Every failure path below (missing file, bad frontmatter, any other
exception) degrades to printing "{}" and exiting 0: no additionalContext, no
stderr noise, session starts clean either way.
"""
import json
import os
import sys


def _root():
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return env_root
    # Fallback for direct runs outside the plugin harness: hooks/session-start.py -> plugin root.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _strip_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return text


def main():
    try:
        root = _root()
        skill_path = os.path.join(root, "skills", "using-leo", "SKILL.md")
        with open(skill_path) as fh:
            raw = fh.read()
        body = _strip_frontmatter(raw)
        body = body.replace("${CLAUDE_PLUGIN_ROOT}", root)
        wrapped = "<leo-policy>\n" + body + "\n</leo-policy>"
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": wrapped,
            }
        }))
    except Exception:
        print("{}")


if __name__ == "__main__":
    main()
    sys.exit(0)
