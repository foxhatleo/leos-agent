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


class RoundTrips(unittest.TestCase):
    """Registering then unregistering a value must give back the original bytes:
    erased characters used to become spaces, and an install/uninstall cycle
    grew the file every time."""

    LAYOUTS = ('{"model":"x"}', '{\n  "model": "x"\n}\n', '{}\n', '{\n  // c\n  "theme": "dark"\n}\n',
               '{\n  "plugin": ["mine"]\n}\n', '{\n  "instructions": [\n    "a"\n  ],\n  "x": 1, // t\n}\n',
               '{"plugin": [], "instructions": []}', '{\n  "x": 1 /* why */\n}\n', '{\n  "x": [\n    1\n  ],\n}\n')

    def cycle(self, text):
        _, spans, _ = edit.properties(text)
        created = [k for k in ("instructions", "plugin") if k not in spans]
        out = text
        for key in ("instructions", "plugin"):
            out = edit.update_array(out, key, ["ours-" + key])
        back = out
        for key in ("instructions", "plugin"):
            back = edit.update_array(back, key, [], ["ours-" + key])
            if key in created:
                back = edit.drop_empty_array(back, key)
        return out, back

    def test_add_then_remove_restores_the_bytes(self):
        for text in self.LAYOUTS:
            with self.subTest(text=text):
                out, back = self.cycle(text)
                self.assertEqual(json.loads(edit.clean(out))["plugin"][-1], "ours-plugin")
                self.assertEqual(back, text)

    def test_insertions_follow_the_last_entry(self):
        out, _ = self.cycle('{\n  "model": "x"\n}\n')
        self.assertEqual(out, '{\n  "model": "x",\n  "instructions": ["ours-instructions"],\n'
                              '  "plugin": ["ours-plugin"]\n}\n')
        out, _ = self.cycle('{"model":"x"}')
        self.assertEqual(out, '{"model":"x", "instructions": ["ours-instructions"], "plugin": ["ours-plugin"]}')
        self.assertEqual(edit.update_array('{"a": [\n    "x"\n  ]}', "a", ["y"]), '{"a": [\n    "x",\n    "y"\n  ]}')

    def test_a_trailing_line_comment_keeps_its_line(self):
        text = '{\n  "x": 1 // note\n}\n'
        out = edit.update_array(text, "plugin", ["p"])
        self.assertIn('"x": 1, // note\n', out)
        self.assertEqual(json.loads(edit.clean(out)), {"x": 1, "plugin": ["p"]})

    def test_no_insertion_lands_inside_a_block_comment(self):
        text = '{\n  "model": "x" /* m\n, ] */ }'
        out = edit.update_array(text, "plugin", ["a"])
        self.assertEqual(json.loads(edit.clean(out)), {"model": "x", "plugin": ["a"]})
        self.assertIn("/* m\n, ] */", out)

    def test_removal_never_joins_code_onto_a_line_comment(self):
        text = '{"a": // c1\n [], "b": 1}'
        out = edit.drop_empty_array(text, "a")
        self.assertEqual(json.loads(edit.clean(out)), {"b": 1})
        self.assertIn("// c1", out)


class DropEmptyArrays(unittest.TestCase):
    """Uninstall must not leave behind a key the installer invented."""

    def test_an_emptied_key_is_removed_and_comments_survive(self):
        text = '{\n  // keep me\n  "theme": "x",\n  "plugin": []\n}\n'
        result = edit.drop_empty_array(text, "plugin")
        self.assertNotIn("plugin", result)
        self.assertIn("// keep me", result)
        self.assertEqual(json.loads(edit.clean(result)), {"theme": "x"})

    def test_comments_around_and_inside_removed_key_survive(self):
        for text in ('{"b": 1, /* before */ "a": [/* inside */], /* after */ "c": 2}',
                     '{/* before */ "a": [/* inside */], /* after */ "c": 2}',
                     '{"b": 1, /* before */ "a": [/* inside */] /* after */}'):
            with self.subTest(text=text):
                result = edit.drop_empty_array(text, "a")
                self.assertNotIn("a", json.loads(edit.clean(result)))
                for comment in ("/* before */", "/* inside */", "/* after */"):
                    self.assertIn(comment, result)

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
