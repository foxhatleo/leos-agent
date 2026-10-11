---
description: Two separable changes with a test as the named check. A mechanical rename across six modules suits a cheap worker; a small function plus its own unit test suits a standard one. Benchmarks cost against the baseline; scored on the acceptance tests passing in a real run, the run's own test, and the acceptance tests left intact.
model: opus
tags: [benchmark, routing, write]
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Agent, Edit, Write, Bash]
---

The `shop` package in your working directory is a small Python service; its tests are in `tests`. Make two independent changes:

1. Rename the setting `MAX_RETRIES` to `RETRY_LIMIT` in `shop/settings.py` and everywhere the package uses it. Keep its value.
2. Implement `retry_delays(limit, base_ms=100, cap_ms=5000)` in `shop/backoff.py`. It returns `limit` delays in milliseconds, starting at `base_ms` and doubling each time, each capped at `cap_ms`. Add at least one unit test of your own for it in a new file, `tests/test_backoff.py`.

`tests/test_contract.py` is read-only and holds the acceptance checks; do not change it. The check for both changes is `python3 -m unittest discover -s tests`, which must pass before you finish.
