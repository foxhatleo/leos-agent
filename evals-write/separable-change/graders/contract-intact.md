---
type: regex
target:
  source: file
  path: tests/test_contract.py
flags: m
pattern: '^    def test_'
match: count:7
---
