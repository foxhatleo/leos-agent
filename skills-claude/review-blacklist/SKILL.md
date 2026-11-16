---
name: review-blacklist
disable-model-invocation: true
description: Show or change the authors whose pull requests watch-review never picks up on its own. One list for every repository on this machine. review-pr still reviews them.
argument-hint: "[add|remove LOGIN ...]"
allowed-tools:
  - Bash(python3 */scripts/watch_review.py blacklist*)
---

# Review author blacklist

Resolve the absolute plugin root from `LEOS_AGENT_ROOT`, `CLAUDE_PLUGIN_ROOT`,
`PLUGIN_ROOT`, or the nearest ancestor of this file containing
`rules/preferences.md`. Run in the current model.

Map the arguments to one command; with none, only show the list:

```
python3 "<plugin-root>/scripts/watch_review.py" blacklist
python3 "<plugin-root>/scripts/watch_review.py" blacklist --add LOGIN
python3 "<plugin-root>/scripts/watch_review.py" blacklist --remove LOGIN
```

Pass GitHub logins as given (several may follow one flag; bots are spelled
`name[bot]`, matching ignores case). If an argument is not a login, ask
rather than guess. Show the resulting list in one line.

A blacklisted author's PRs are not emitted by a running or future watch,
including ones it already followed. Running `/review-pr` on one marks it
watched, and from then on it is followed as usual. The user's own PRs are
never reviewed, blacklist or not.
