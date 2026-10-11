#!/usr/bin/env python3
"""The skills the OpenCode 2 plugin registers, as JSON on stdout.

The same set and content leo-install.py copies for OpenCode 1, from the same
rendering, shaped as OpenCode 2's Skill.Info: id (the directory a copy would
sit in), name, description, autoinvoke (false for a user-invoked skill), path
(the source SKILL.md, so its reference/ files resolve beside it) and content
(the body after the frontmatter, with the plugin root baked in).
"""
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
FRONTMATTER = re.compile(r"---\n(.*?)\n---\n?(.*)", re.DOTALL)


def _installer():
    spec = importlib.util.spec_from_file_location("leo_install", ROOT / "scripts" / "leo-install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _field(frontmatter, name):
    match = re.search(rf"(?m)^{re.escape(name)}:\s*(.+?)\s*$", frontmatter)
    if not match:
        return None
    value = match.group(1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def skill(skill_id, src, text):
    match = FRONTMATTER.match(text)
    if not match:
        raise ValueError(f"{src} has no frontmatter")
    frontmatter, body = match.groups()
    info = {"id": skill_id, "name": _field(frontmatter, "name") or skill_id, "path": str(src), "content": body}
    description = _field(frontmatter, "description")
    if description:
        info["description"] = description
    if (_field(frontmatter, "disable-model-invocation") or "").lower() == "true":
        info["autoinvoke"] = False
    return info


def skills(root=ROOT):
    installer = _installer()
    root = Path(root)
    entries = [("leo-install", root / "skills" / "install" / "SKILL.md", True)]
    entries += [(name, root / "skills" / name / "SKILL.md", False) for name in installer.OPENCODE_SKILLS]
    return [skill(skill_id, src, installer.opencode_payload(src, root, rename_install=rename))
            for skill_id, src, rename in entries]


def main():
    json.dump(skills(), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
