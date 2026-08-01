#!/usr/bin/env python3
"""SessionStart hook: inject the using-leo policy skill as session context.

Serves three harnesses from one script: Claude Code, Codex CLI, and Cursor.
Harness detection is env-var based (see _detect_harness); each harness gets
the same policy body plus a harness-specific mapping appendix (which tier
name means which concrete model, which tool does what) — the mapping is
what makes the tier-labeled policy body concretely actionable on that
harness, so appending it is load-bearing, not decorative.

A hook failure here would otherwise break every session start (startup,
resume, /clear, /compact) for a policy-injection convenience — that trade is
never worth it. Every failure path below (missing root, missing SKILL.md,
missing mapping file, bad frontmatter, any other exception) degrades to
printing "{}" and exiting 0: no additionalContext, no stderr noise, session
starts clean either way, on any of the three harnesses. The reason is
appended to local/session-start.log so a dead policy is still diagnosable.
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
    # Codex sets PLUGIN_ROOT *and* CLAUDE_PLUGIN_ROOT — the latter deliberately,
    # "for compatibility with existing plugin hooks". So absence of
    # CLAUDE_PLUGIN_ROOT is NOT a Codex signal; testing for it shipped Codex the
    # Claude mapping (models Codex cannot run) for every session. Presence of the
    # unprefixed PLUGIN_ROOT is the real signal: Claude Code sets only its own
    # prefixed variable. Deliberately no CODEX_* sniffing on top — an unrelated
    # CODEX_* var exported in a Claude shell would then hijack a Claude session,
    # and PLUGIN_ROOT alone already resolves Codex correctly.
    if os.environ.get("PLUGIN_ROOT"):
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


def _breadcrumb(exc):
    """Record why injection failed. Never raises: this runs on the fail path."""
    try:
        base = os.environ.get("LEOS_AGENT_LOCAL_PATH") or os.path.join(
            os.path.expanduser("~"), ".leos-agent-local"
        )
        local = base
        os.makedirs(local, exist_ok=True)
        with open(os.path.join(local, "session-start.log"), "a", encoding="utf-8") as fh:
            fh.write("policy injection skipped: {}: {}\n".format(type(exc).__name__, exc))
    except Exception:
        pass


def _memory_block(root):
    """Refresh the store, project it, and return the index for this project.

    Imported rather than spawned: this hook has a 10-second budget and a
    subprocess would spend a chunk of it on interpreter startup alone.
    """
    scripts = os.path.join(root, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import memory

    return memory.session(os.getcwd())


def main():
    try:
        root = _root()
        harness = _detect_harness()

        skill_path = os.path.join(root, "skills", "using-leo", "SKILL.md")
        # encoding is explicit on every read: the policy is full of em-dashes and
        # arrows, so under a non-UTF-8 locale (LC_ALL=C — routine in cron, CI,
        # containers, plain ssh) the platform default decoder raises and the
        # whole policy silently vanishes. Failing open makes that invisible.
        with open(skill_path, encoding="utf-8") as fh:
            raw = fh.read()
        body = _strip_frontmatter(raw)

        mapping_path = os.path.join(
            root, "skills", "using-leo", "references", harness + "-mapping.md"
        )
        with open(mapping_path, encoding="utf-8") as fh:
            mapping = fh.read()
        body = body.rstrip("\n") + "\n\n" + mapping.rstrip("\n") + "\n"
        # Substitute AFTER the append so placeholders inside the mapping
        # (e.g. the claude-mapping workflow path) resolve too.
        body = body.replace("${CLAUDE_PLUGIN_ROOT}", root)

        wrapped = "<leo-policy>\n" + body + "\n</leo-policy>"

        # Memory rides in its own envelope, appended after substitution: it is
        # data, not policy, and keeping the two separable lets each be measured
        # against the budget on its own. The nested handler is load-bearing —
        # the outer one drops the entire policy, and a broken memory store must
        # never cost the session its operating instructions.
        try:
            memory_block = _memory_block(root)
        except Exception as exc:
            _breadcrumb(exc)
            memory_block = ""
        if memory_block:
            wrapped += "\n\n<leo-memory>\n" + memory_block.rstrip("\n") + "\n</leo-memory>"

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
    except Exception as exc:
        # Still fail open — but leave a trace. Without one, a silently dead
        # policy is indistinguishable from a working one for as long as it
        # takes someone to notice the behavior change.
        _breadcrumb(exc)
        print("{}")


if __name__ == "__main__":
    main()
    sys.exit(0)
