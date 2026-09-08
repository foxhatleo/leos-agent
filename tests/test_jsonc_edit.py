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


class DropEmptyArrays(unittest.TestCase):
    """Uninstall must not leave behind a key the installer invented."""

    def test_an_emptied_key_is_removed_and_comments_survive(self):
        text = '{\n  // keep me\n  "theme": "x",\n  "plugin": []\n}\n'
        result = edit.drop_empty_array(text, "plugin")
        self.assertNotIn("plugin", result)
        self.assertIn("// keep me", result)
        self.assertEqual(json.loads(edit.clean(result)), {"theme": "x"})

    def test_a_key_that_still_holds_something_is_never_touched(self):
        text = '{"plugin": ["mine"], "theme": "x"}'
        self.assertEqual(edit.drop_empty_array(text, "plugin"), text)
        self.assertEqual(edit.drop_empty_array(text, "absent"), text)

    def test_removal_leaves_valid_json_in_every_position(self):
        for text in ('{"a": [], "b": 1}', '{"b": 1, "a": []}', '{"a": []}',
                     '{"b": 1, "a": [], "c": 2}'):
            with self.subTest(text=text):
                result = edit.drop_empty_array(text, "a")
                parsed = json.loads(edit.clean(result))
                self.assertNotIn("a", parsed)
                self.assertEqual(len(parsed), len(json.loads(edit.clean(text))) - 1)


if __name__ == "__main__":
    unittest.main()
