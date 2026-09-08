import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jsonc_edit as edit


class JsoncEdits(unittest.TestCase):
    def test_comments_strings_and_trailing_commas_survive(self):
        text = '{\n// keep\n"name": "literal ,} // text",\n"instructions": ["user", /* keep too */ "old",],\n}\n'
        result = edit.update_array(text, "instructions", ["new"], ["old"])
        data = json.loads(edit.clean(result))
        self.assertEqual(data["instructions"], ["user", "new"])
        self.assertEqual(data["name"], "literal ,} // text")
        self.assertIn("// keep", result)
        self.assertIn("/* keep too */", result)
        self.assertEqual(edit.update_array(result, "instructions", ["new"]), result)

    def test_append_property_with_or_without_trailing_comma(self):
        for text in ('{}', '{"x": 1}', '{"x": 1, /* comment */ }'):
            result = edit.update_array(text, "plugin", ["leos-agent"])
            self.assertEqual(json.loads(edit.clean(result))["plugin"], ["leos-agent"])

    def test_remove_every_combination_of_array_elements(self):
        for mask in range(8):
            text = '{"instructions": ["a", /* a */ "b", // b\n "c",]}'
            removed = [v for i, v in enumerate("abc") if mask & (1 << i)]
            result = edit.update_array(text, "instructions", removals=removed)
            self.assertEqual(json.loads(edit.clean(result))["instructions"], [v for v in "abc" if v not in removed])
            self.assertIn("/* a */", result)
            self.assertIn("// b", result)

    def test_malformed_shapes_and_duplicate_keys_are_refused(self):
        for text in ('{"x":1,"x":2}', '{"instructions":false}', '{"instructions":[{}]}', '[]'):
            with self.assertRaises(ValueError):
                edit.update_array(text, "instructions", ["a"])
