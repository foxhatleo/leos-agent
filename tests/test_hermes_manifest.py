"""plugin.yaml must declare what register() registers, as `hermes plugins validate` checks."""

import importlib.util
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def manifest_list(text, key):
    """A top-level YAML list field: `key: []` or `key:` followed by `  - item` lines.

    Returns None for any other shape (a scalar such as `true` included), which
    Hermes reads as an empty declaration.
    """
    inline = re.search(rf"(?m)^{key}:[ \t]*\[(.*)\][ \t]*$", text)
    if inline:
        return [item.strip().strip("'\"") for item in inline.group(1).split(",") if item.strip()]
    block = re.search(rf"(?m)^{key}:[ \t]*\n((?:[ \t]+-[ \t]*\S.*\n?)+)", text)
    if block:
        return [line.split("-", 1)[1].strip().strip("'\"") for line in block.group(1).splitlines() if line.strip()]
    return None


class Recorder:
    """The parts of Hermes's plugin context register() touches, recording names."""

    def __init__(self):
        self.hooks, self.tools = [], []

    def register_hook(self, name, callback):
        self.hooks.append(name)

    def register_tool(self, name, *args, **kwargs):
        self.tools.append(name)

    def register_skill(self, *args, **kwargs):
        pass

    def register_command(self, *args, **kwargs):
        pass

    def register_system_prompt_section(self, *args, **kwargs):
        pass


class HermesManifest(unittest.TestCase):
    def test_declared_hooks_and_tools_match_registrations(self):
        spec = importlib.util.spec_from_file_location("hermes_manifest_adapter", ROOT / "__init__.py")
        adapter = importlib.util.module_from_spec(spec)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": tmp}):
            spec.loader.exec_module(adapter)
            ctx = Recorder()
            adapter.register(ctx)
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        hooks, tools = manifest_list(text, "provides_hooks"), manifest_list(text, "provides_tools")
        self.assertIsNotNone(hooks, "provides_hooks must be a list of hook names")
        self.assertIsNotNone(tools, "provides_tools must be a list of tool names")
        self.assertEqual(sorted(hooks), sorted(set(ctx.hooks)))
        self.assertEqual(sorted(tools), sorted(set(ctx.tools)))


if __name__ == "__main__":
    unittest.main()
