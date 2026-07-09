#!/usr/bin/env python3
"""Guard the enforced-vs-advisory policy boundary.

Asserts that the Codex and Cursor rendered configs never contain a literal Claude-style permission
string (`Bash(...)` / `permissions.allow` in Claude's exact form) — the exact contamination the
steelman warned about. Codex's command policy is advisory; Cursor uses Shell()/Read() tokens, not
Claude's Bash(). A future "unify the policy step" edit that pasted Claude strings across would be
caught here. Run: python3 tests/contamination-check.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

problems = []


def text_of(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


# Codex config/policy must NOT carry Claude's Bash(...:*) permission tokens.
for p in ["tools/codex/command-policy-notes.json", "tools/codex/config-fragment.toml",
          "tools/codex/hooks.json"]:
    t = text_of(p)
    if "Bash(" in t or '"permissions"' in t and "allow" in t and "Bash(" in t:
        problems.append(f"{p}: contains a Claude-style Bash(...) permission token")

# Cursor uses Shell()/Read() tokens — it must NOT carry Claude's Bash(...) tokens.
for p in ["tools/cursor/permissions-fragment.json"]:
    t = text_of(p)
    if "Bash(" in t:
        problems.append(f"{p}: contains a Claude-style Bash(...) token (Cursor uses Shell())")

# The advisory Codex renderer must self-label as advisory (not imply enforced parity).
notes = json.load(open(os.path.join(ROOT, "tools/codex/command-policy-notes.json")))
if "ADVISORY" not in json.dumps(notes).upper():
    problems.append("tools/codex/command-policy-notes.json: missing an ADVISORY enforcement label")

# The shared policy data must carry per-renderer enforcement labels.
pol = json.load(open(os.path.join(ROOT, "core/policy/policy-data.json")))
for host in ["claude", "codex", "opencode", "cursor"]:
    r = pol.get("renderers", {}).get(host, {})
    if "enforcement" not in json.dumps(r):
        problems.append(f"policy-data.json: renderer '{host}' missing an enforcement label")

if problems:
    for p in problems:
        print("FAIL:", p)
    print(f"contamination-check: {len(problems)} problem(s)")
    sys.exit(1)
print("contamination-check: OK — no enforced/advisory boundary violations")
