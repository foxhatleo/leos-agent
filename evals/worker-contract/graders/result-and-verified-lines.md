---
type: regex
target: trace
arm: with-only
pattern: '(?:\\n|\n|")[*_`># -]*Result:[*_`]*[ \t]*(?:done|partial|blocked|escalate)[*_`.]*[ \t]*(?:\\n|\n)+[*_`># -]*Verified:[*_`]*[ \t]*[^<\s\\"]'
---
