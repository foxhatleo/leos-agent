#!/usr/bin/env python3
"""watch_review: stream new GitHub review requests without spending tokens.

The discovery half of the review watcher is a fixed query, a fixed filter, and
a state file — none of it needs a model. This script does that half in the
shell and prints one line per new pull request; whoever reads stdout does the
review. An idle tick costs one `gh` call and zero tokens.

  watch_review.py monitor [-C DIR] --interval 300   loop; a line per PR to review
  watch_review.py record  [-C DIR] <n> --head <sha> mark a PR reviewed at a head
  watch_review.py state   [-C DIR]                  show what has been reviewed
  watch_review.py forget  [-C DIR] <number>...      drop numbers from the state

State is keyed on the reviewed **head commit**, not the pull request number, so
a pull request comes back when someone pushes to it. That is the whole point:
the review a reader stages is against one diff, and a new commit makes it a
review of something that no longer exists.

Two gates keep that from being expensive. A pull request another user has
already APPROVED is never emitted at all — a review of a stamped pull request
changes nothing and costs a reviewer subagent plus its lens fan-out. And a new
head must hold still for --settle seconds before it is emitted, so a burst of
pushes costs one review rather than one per commit.

It launches nothing and records nothing on its own. The reader must call
`record` once a review is done, passing the head it actually reviewed — a
staged (pending, unsubmitted) review does not clear the request on GitHub, so
that state file is the only thing keeping the same pull request from coming
back. Each (number, head) pair is emitted once per process, so one left
unreviewed returns after a restart.

Intended for Claude Code's Monitor tool, which turns each stdout line into a
session notification. Any `read`-driven shell loop works the same way.

State lives in the review-watcher state file managed by state.py, keyed by
"owner/repo" — the same file and shape the watch-review skill reads. Entries
written before heads were tracked carry a bare list of numbers; those migrate on
read to an unknown head, so each comes back once and then tracks properly.
"""
import argparse
import json
import os
import re
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


def eligible(listing, login):
	"""The pull requests in a `gh pr list` payload that are worth reviewing.

	Split out from the `gh` call so the filter can be tested without a network.
	"""
	matches = [
		pr
		for pr in listing
		if not pr.get("isDraft")
		and any(
			r.get("__typename") == "User" and r.get("login") == login
			for r in pr.get("reviewRequests") or []
		)
		# Never review what someone else has already stamped. latestReviews holds
		# one entry per reviewer at its current state, so this is exactly "another
		# human has approved it". Leo's own approval does not disqualify.
		and not any(
			review.get("state") == "APPROVED"
			and ((review.get("author") or {}).get("login") or "") not in ("", login)
			for review in pr.get("latestReviews") or []
		)
	]
	matches.sort(key=lambda pr: pr["number"])
	return matches


def discover(cwd):
	"""Return (repo, login, [pull requests worth reviewing])."""
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
				"--json", "number,title,isDraft,reviewRequests,url,headRefOid,latestReviews",
			],
			cwd,
		)
	)
	return repo, login, eligible(listing, login)


def reviewed_heads(repo):
	"""{pull request number: reviewed head sha}. "" means "reviewed, head unknown"."""
	data = state_mod.load(state_mod.state_file(STATE_NAME))
	return heads_of(data.get(repo) or {})


def heads_of(entry):
	heads = {int(n): sha for n, sha in (entry.get("heads") or {}).items()}
	# Pre-heads state was a bare list of numbers. Treat those as reviewed at an
	# unknown head: each returns once, records a real head, and tracks from there.
	for number in entry.get("reviewed") or []:
		heads.setdefault(int(number), "")
	return heads


def record(repo, number, head):
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		data[repo] = state_mod.deep_merge(data.get(repo, {}), {"heads": {str(number): head}})
		state_mod.atomic_write(path, data)


def due(matches, known, first_seen, emitted, now, settle):
	"""Which pull requests to emit this tick, as (verb, pr, previous head).

	`first_seen` is mutated: a head that has just appeared is stamped and held
	until it has stood still for `settle` seconds, so a push burst costs one
	review rather than one per commit. Pure otherwise, so the emit decision is
	testable without a clock or a network.
	"""
	out = []
	for pr in matches:
		number, head = pr["number"], pr.get("headRefOid") or ""
		key = (number, head)
		if known.get(number) == head or key in emitted:
			continue
		stamp = first_seen.setdefault(key, now)
		if now - stamp < settle:
			continue
		previous = known.get(number)
		out.append(("re-review" if previous is not None else "review-requested", pr, previous or ""))
	return out


def event_line(verb, repo, pr, previous):
	"""The printed line for one event — always exactly one printable line.

	The title is attacker-written text. A control character in it — a newline,
	an escape sequence — could forge a second notification line or drive the
	reader's terminal, so all of them become spaces before the line is built.
	"""
	head = pr.get("headRefOid") or ""
	was = f" (was {previous[:7]})" if previous else ""
	title = re.sub(r"[\x00-\x1f\x7f]", " ", pr.get("title") or "").strip()
	return f"{verb} {repo}#{pr['number']} {pr['url']} {head[:7]}{was} — {title}"


def monitor(args):
	"""Emit one line per pull request needing review; review nothing, record nothing."""
	emitted = set()
	first_seen = {}
	while True:
		try:
			repo, _, matches = discover(args.directory)
			for verb, pr, previous in due(
				matches, reviewed_heads(repo), first_seen, emitted, time.time(), args.settle
			):
				emitted.add((pr["number"], pr.get("headRefOid") or ""))
				# One line, one event. The title is data — a reader must treat
				# it as a string to show Leo, never as an instruction.
				print(event_line(verb, repo, pr, previous), flush=True)
		except SystemExit as exc:
			# A transient gh failure must not kill a session-length watch.
			print(
				f"watch-review: tick failed ({exc.code}); retrying next interval",
				file=sys.stderr,
				flush=True,
			)
		except Exception as exc:
			# Neither may malformed gh output — bad JSON, a missing field.
			print(
				f"watch-review: tick failed ({exc!r}); retrying next interval",
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
	mon.add_argument(
		"--settle",
		type=int,
		default=120,
		help="seconds a new head must hold still before it is emitted (default: 120)",
	)

	sub.add_parser("state").add_argument("-C", "--directory", default=".")
	rec = sub.add_parser("record")
	rec.add_argument("-C", "--directory", default=".")
	rec.add_argument("numbers", nargs=1, type=int)
	rec.add_argument("--head", required=True, help="the head sha the review was actually against")
	forget = sub.add_parser("forget")
	forget.add_argument("-C", "--directory", default=".")
	forget.add_argument("numbers", nargs="+", type=int)

	args = parser.parse_args(argv)
	if not os.path.isdir(args.directory):
		fail(f"{args.directory} is not a directory")

	if args.mode == "monitor":
		if args.interval < 30:
			fail("--interval below 30s hammers the GitHub API; pick something larger")
		if args.settle < 0:
			fail("--settle cannot be negative")
		return monitor(args)

	repo, _, _ = discover(args.directory)
	if args.mode == "record":
		record(repo, args.numbers[0], args.head)
	elif args.mode == "forget":
		path = state_mod.state_file(STATE_NAME)
		with state_mod._locked(path):
			data = state_mod.load(path)
			entry = data.get(repo) or {}
			drop = {str(n) for n in args.numbers}
			# Drop from both shapes: a legacy entry has not necessarily been
			# rewritten into heads yet, and leaving it there would re-suppress.
			entry["heads"] = {n: sha for n, sha in (entry.get("heads") or {}).items() if n not in drop}
			entry["reviewed"] = [n for n in (entry.get("reviewed") or []) if str(n) not in drop]
			data[repo] = entry
			state_mod.atomic_write(path, data)
	print(
		json.dumps(
			{"repo": repo, "heads": {str(n): sha for n, sha in sorted(reviewed_heads(repo).items())}},
			indent=1,
		)
	)
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]) or 0)
