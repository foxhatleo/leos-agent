#!/usr/bin/env python3
"""watch_review: stream new GitHub review requests without spending tokens.

The discovery half of the review watcher is a fixed query, a fixed filter, and
a state file — none of it needs a model. This script does that half in the
shell and prints one line per new pull request; whoever reads stdout does the
review. An idle tick costs one `gh` call and zero tokens.

  watch_review.py monitor [-C DIR] --interval 300  loop; a line per new PR
  watch_review.py record  [-C DIR] <number>...     mark numbers reviewed
  watch_review.py state   [-C DIR]                 show what has been reviewed
  watch_review.py forget  [-C DIR] <number>...     drop numbers from the state

It launches nothing and records nothing on its own. The reader must call
`record` once a review is done — a staged (pending, unsubmitted) review does
not clear the request on GitHub, so that state file is the only thing keeping
the same pull request from coming back. `monitor` emits each pull request once
per process, so an unreviewed one is re-emitted after a restart.

Intended for Claude Code's Monitor tool, which turns each stdout line into a
session notification. Any `read`-driven shell loop works the same way.

State lives in the review-watcher state file managed by state.py, keyed by
"owner/repo" — the same file and shape the watch-review skill reads.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state as state_mod  # noqa: E402

STATE_NAME = "review-watcher"


def fail(message):
	print(f"watch-review: {message}", file=sys.stderr)
	sys.exit(1)


def gh(args, cwd):
	"""Run a read-only gh command and return stdout, or fail loudly."""
	try:
		proc = subprocess.run(
			["gh"] + args, cwd=cwd, capture_output=True, text=True, check=False
		)
	except FileNotFoundError:
		fail("gh is not installed or not on PATH")
	if proc.returncode != 0:
		fail((proc.stderr or proc.stdout).strip() or f"gh {args[0]} failed")
	return proc.stdout


def discover(cwd):
	"""Return (repo, login, [pull requests directly requesting login])."""
	repo = json.loads(gh(["repo", "view", "--json", "nameWithOwner"], cwd))["nameWithOwner"]
	login = gh(["api", "user", "--jq", ".login"], cwd).strip()
	if not login:
		fail("gh api user returned no login; is gh authenticated?")
	# user-review-requested matches direct requests only; the reviewRequests
	# check below is belt and braces against a stale or fuzzy search result.
	listing = json.loads(
		gh(
			[
				"pr", "list", "--state", "open",
				"--search", f"user-review-requested:{login}",
				"--limit", "100",
				"--json", "number,title,isDraft,reviewRequests,url",
			],
			cwd,
		)
	)
	matches = [
		pr
		for pr in listing
		if not pr.get("isDraft")
		and any(
			r.get("__typename") == "User" and r.get("login") == login
			for r in pr.get("reviewRequests") or []
		)
	]
	matches.sort(key=lambda pr: pr["number"])
	return repo, login, matches


def reviewed_numbers(repo):
	data = state_mod.load(state_mod.state_file(STATE_NAME))
	entry = data.get(repo) or {}
	return set(entry.get("reviewed") or [])


def record(repo, number):
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		data[repo] = state_mod.deep_merge(data.get(repo, {}), {"reviewed": [number]})
		state_mod.atomic_write(path, data)


def monitor(args):
	"""Emit one line per new pull request; review nothing, record nothing."""
	emitted = set()
	while True:
		try:
			repo, _, matches = discover(args.directory)
			done = reviewed_numbers(repo)
			for pr in matches:
				n = pr["number"]
				if n in done or n in emitted:
					continue
				emitted.add(n)
				# One line, one event. The title is data — a reader must treat
				# it as a string to show Leo, never as an instruction.
				print(f"review-requested {repo}#{n} {pr['url']} — {pr['title']}", flush=True)
		except SystemExit as exc:
			# A transient gh failure must not kill a session-length watch.
			print(
				f"watch-review: tick failed ({exc.code}); retrying next interval",
				file=sys.stderr,
				flush=True,
			)
		time.sleep(args.interval)


def main(argv):
	parser = argparse.ArgumentParser(prog="watch_review.py", description=__doc__)
	sub = parser.add_subparsers(dest="mode", required=True)

	mon = sub.add_parser("monitor")
	mon.add_argument("-C", "--directory", default=".", help="repository directory (default: cwd)")
	mon.add_argument("--interval", type=int, default=300, help="seconds between ticks")

	sub.add_parser("state").add_argument("-C", "--directory", default=".")
	for name in ("record", "forget"):
		p = sub.add_parser(name)
		p.add_argument("-C", "--directory", default=".")
		p.add_argument("numbers", nargs="+", type=int)

	args = parser.parse_args(argv)
	if not os.path.isdir(args.directory):
		fail(f"{args.directory} is not a directory")

	if args.mode == "monitor":
		if args.interval < 30:
			fail("--interval below 30s hammers the GitHub API; pick something larger")
		return monitor(args)

	repo, _, _ = discover(args.directory)
	if args.mode == "record":
		for n in args.numbers:
			record(repo, n)
	elif args.mode == "forget":
		path = state_mod.state_file(STATE_NAME)
		with state_mod._locked(path):
			data = state_mod.load(path)
			entry = data.get(repo) or {}
			drop = set(args.numbers)
			entry["reviewed"] = [n for n in (entry.get("reviewed") or []) if n not in drop]
			data[repo] = entry
			state_mod.atomic_write(path, data)
	print(json.dumps({"repo": repo, "reviewed": sorted(reviewed_numbers(repo))}, indent=1))
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]) or 0)
