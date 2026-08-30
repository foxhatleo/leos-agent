#!/usr/bin/env python3
"""handoff: machine-local session handoff documents for leos-agent.

A handoff is a markdown file one session writes and another session — in this
harness or a different one — reads to pick the work back up. This script owns
naming, paths, and listing only. It never composes prose: the model writes the
body, because summarising a session is the one part of this that needs a model.

DATA lives under the same root as state.py, ${LEOS_AGENT_LOCAL_PATH:-
$HOME/.leos-agent-local}, never derived from this file's own location — a
plugin update can wipe or relocate the code, and must never take handoffs with
it. Files land at <root>/handoffs/<name>.md.

  handoff.py new  <slug>              print the de-collided name and its path
  handoff.py path <name>              resolve a name (exact, else unique prefix)
  handoff.py list [--all] [--limit N] recent handoffs, newest first
  handoff.py rm   <name>              delete one

Nothing is ever pruned automatically; `rm` is the only way a handoff goes away.
`list` shows only handoffs written in or under the current directory unless
--all is passed; when that leaves nothing it falls back to showing all of them
rather than sending the caller away to re-run. Exit codes: 0 ok, non-zero on error.
"""
import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from state import _data_root  # noqa: E402  same data root, deliberately shared

SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def handoff_dir():
    # 0700 on creation, matching state.py: handoffs carry project context that
    # is nobody else's business on a shared machine. The root is created first
    # so it gets the mode too — makedirs applies mode to the leaf only.
    root = _data_root()
    os.makedirs(root, mode=0o700, exist_ok=True)
    path = os.path.join(root, "handoffs")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def file_for(name):
    return os.path.join(handoff_dir(), f"{name}.md")


def frontmatter(path):
    """Parse the leading --- block. Returns {} for a file without one."""
    data = {}
    try:
        with open(path) as fh:
            if fh.readline().strip() != "---":
                return data
            for line in fh:
                if line.strip() == "---":
                    break
                key, sep, value = line.partition(":")
                if sep:
                    data[key.strip()] = value.strip()
    except OSError:
        return {}
    return data


def title_of(path):
    """The first `# ` heading — after the frontmatter when there is one, from
    the top of the file when there is not."""
    try:
        with open(path) as fh:
            first = fh.readline()
            if first.strip() == "---":
                for line in fh:
                    if line.strip() == "---":
                        break
            elif first.startswith("# "):
                return first[2:].strip()
            for line in fh:
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return ""


def age_of(created):
    try:
        stamp = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "?"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    delta = dt.datetime.now(dt.timezone.utc) - stamp
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)}m"
    if minutes < 60 * 48:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def entries():
    out = []
    for entry in os.scandir(handoff_dir()):
        if entry.is_file() and entry.name.endswith(".md"):
            name = entry.name[:-3]
            meta = frontmatter(entry.path)
            out.append((name, entry.path, meta, entry.stat().st_mtime))
    out.sort(key=lambda row: row[3], reverse=True)
    return out


def resolve(name):
    exact = file_for(name)
    if os.path.isfile(exact):
        return name, exact
    matches = [row for row in entries() if row[0].startswith(name)]
    if len(matches) == 1:
        return matches[0][0], matches[0][1]
    if not matches:
        sys.exit(f"handoff: no handoff named {name!r}. `handoff.py list --all` shows what exists.")
    sys.exit("handoff: {!r} is ambiguous — {}".format(name, ", ".join(row[0] for row in matches)))


def cmd_new(argv):
    if len(argv) != 1:
        sys.exit("handoff: usage: handoff.py new <slug>")
    slug = argv[0]
    if not SLUG.match(slug) or not 3 <= len(slug) <= 60:
        sys.exit(f"handoff: {slug!r} is not a valid slug (lowercase, digits and hyphens, 3-60 chars)")
    name, suffix = slug, 2
    while os.path.exists(file_for(name)):
        name = f"{slug}-{suffix}"
        suffix += 1
    print(name)
    print(file_for(name))
    # The `created:` value, ready to copy verbatim — a model asked to invent
    # "now" gets it wrong often enough to matter.
    print(dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def cmd_path(argv):
    if len(argv) != 1:
        sys.exit("handoff: usage: handoff.py path <name>")
    print(resolve(argv[0])[1])


def cmd_list(argv):
    show_all = "--all" in argv
    limit = 20
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (IndexError, ValueError):
            sys.exit("handoff: --limit needs a number")
    here = os.path.realpath(os.getcwd())

    def collect(unfiltered):
        rows = []
        for name, path, meta, _ in entries():
            cwd = os.path.realpath(meta.get("cwd", "")) if meta.get("cwd") else ""
            if not unfiltered and cwd:
                related = here == cwd or here.startswith(cwd + os.sep) or cwd.startswith(here + os.sep)
                if not related:
                    continue
            rows.append((name, age_of(meta.get("created", "")), meta.get("repo", meta.get("cwd", "?")), title_of(path)))
            if len(rows) >= limit:
                break
        return rows

    rows = collect(show_all)
    note = ""
    if not rows and not show_all:
        # Falling back beats printing "try --all": the caller is usually a model
        # one round trip from giving up and searching the filesystem instead.
        rows = collect(True)
        note = "none written in or under this directory; showing all"
    if not rows:
        print("no handoffs")
        return
    if note:
        print(note)
    width = max(len(row[0]) for row in rows)
    for name, age, repo, title in rows:
        print(f"{name.ljust(width)}  {age.rjust(4)}  {repo}  {title}")


def cmd_rm(argv):
    if len(argv) != 1:
        sys.exit("handoff: usage: handoff.py rm <name>")
    name, path = resolve(argv[0])
    os.unlink(path)
    print(f"removed {name}")


def main(argv):
    commands = {"new": cmd_new, "path": cmd_path, "list": cmd_list, "rm": cmd_rm}
    if not argv or argv[0] not in commands:
        sys.exit(__doc__.strip())
    commands[argv[0]](argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
