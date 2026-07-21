#!/usr/bin/env python3
"""SessionStart hook: inject the using-leo policy skill as session context.

Serves three harnesses from one script: Claude Code, Codex CLI, and Cursor.
Harness detection is env-var based (see _detect_harness); each harness gets
the same policy body plus a harness-specific mapping appendix (which tier
name means which concrete model, which tool does what) — the mapping is
what makes the tier-labeled policy body concretely actionable on that
harness, so appending it is load-bearing, not decorative.

A hook failure here would otherwise break every session start (startup,
/clear, /compact) for a policy-injection convenience — that trade is never
worth it. Every failure path below (missing root, missing SKILL.md, missing
mapping file, bad frontmatter, any other exception) degrades to printing
"{}" and exiting 0: no additionalContext, no stderr noise, session starts
clean either way, on any of the three harnesses.
"""
import json
import os
import sys


def _root():
    for var in ("CURSOR_PLUGIN_ROOT", "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        env_root = os.environ.get(var)
        if env_root:
            return env_root
    # Fallback for direct runs outside the plugin harness: hooks/session-start.py -> plugin root.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _detect_harness():
    # Order matters: Cursor may set both CURSOR_PLUGIN_ROOT and PLUGIN_ROOT,
    # so the cursor check must come first.
    if os.environ.get("CURSOR_PLUGIN_ROOT") or os.environ.get("CURSOR_VERSION"):
        return "cursor"
    if os.environ.get("PLUGIN_ROOT") and not os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "codex"
    return "claude"


def _strip_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return text


def _apply_claude_model_options(text):
    for tier in ("fable", "opus", "sonnet", "haiku"):
        placeholder = "${user_config." + tier + "_model}"
        option = os.environ.get("CLAUDE_PLUGIN_OPTION_" + tier.upper() + "_MODEL")
        if option:
            text = text.replace(placeholder, option)
    return text


def main():
    try:
        root = _root()
        harness = _detect_harness()

        skill_path = os.path.join(root, "skills", "using-leo", "SKILL.md")
        with open(skill_path) as fh:
            raw = fh.read()
        body = _strip_frontmatter(raw)

        mapping_path = os.path.join(
            root, "skills", "using-leo", "references", harness + "-mapping.md"
        )
        with open(mapping_path) as fh:
            mapping = fh.read()
        body = body.rstrip("\n") + "\n\n" + mapping.rstrip("\n") + "\n"
        if harness == "claude":
            body = _apply_claude_model_options(body)
        # Substitute AFTER the append so placeholders inside the mapping
        # (e.g. the claude-mapping workflow path) resolve too.
        body = body.replace("${CLAUDE_PLUGIN_ROOT}", root)

        wrapped = "<leo-policy>\n" + body + "\n</leo-policy>"

        if harness == "cursor":
            output = {"additional_context": wrapped}
        else:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": wrapped,
                }
            }
        print(json.dumps(output))
    except Exception:
        print("{}")


if __name__ == "__main__":
    main()
    sys.exit(0)
