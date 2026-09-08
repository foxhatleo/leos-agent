#!/usr/bin/env python3
"""watch_review: stream new GitHub review requests without spending tokens.

The discovery half of the review watcher is a fixed query, a fixed filter, and
a state file — none of it needs a model. This script does that half in the
shell and prints one line per new pull request; whoever reads stdout does the
review. Idle ticks use only the GitHub API; pagination increases calls for large repositories.

  watch_review.py monitor [-C DIR] --interval 300
  watch_review.py record [-C DIR] <n> --head <full-sha> --result <stage-report.json> --claim <token>
  watch_review.py renew|release [-C DIR] <n> --claim <token>
  watch_review.py state|forget [-C DIR] [numbers...]

A head must settle before emission. Cross-process leases prevent duplicate
workers; renew every 15 minutes during a long review. Expired or released
claims retry up to three times per head, then require explicit `forget`.
Emission is not completion. Only a successful pinned review report can record
a head as reviewed. A new push is eligible again. Drafts, team-only requests,
and PRs already approved by another user are excluded.

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
import uuid

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
			["gh"] + args, cwd=cwd, capture_output=True, text=True, check=False, timeout=30
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


REQUEST_FIELDS = "requestedReviewer { __typename ... on User { login } }"
REVIEW_FIELDS = "state author { login }"
PAGE_FIELDS = "pageInfo { hasNextPage endCursor }"
PR_FIELDS = """id number title isDraft url headRefOid
 reviewRequests(first:100) { nodes { %s } %s }
 latestReviews(first:100) { nodes { %s } %s }""" % (REQUEST_FIELDS, PAGE_FIELDS, REVIEW_FIELDS, PAGE_FIELDS)


def identity(cwd):
	repo = json.loads(gh(["repo", "view", "--json", "nameWithOwner"], cwd))["nameWithOwner"]
	login = gh(["api", "user", "--jq", ".login"], cwd).strip()
	if not login:
		fail("gh api user returned no login; is gh authenticated?")
	return repo, login


def graphql(query, variables, cwd):
	args = ["api", "graphql", "-f", "query=" + query]
	for key, value in variables.items():
		if value is not None:
			args.extend(["-f", key + "=" + str(value)])
	data = json.loads(gh(args, cwd))
	if data.get("errors"):
		raise ValueError("GitHub GraphQL failed: " + json.dumps(data["errors"]))
	return data["data"]


def connection_nodes(first, next_page):
	"""Read every page; a repeated or absent continuation is an error."""
	page, seen = first, set()
	while True:
		yield from page["nodes"]
		info = page["pageInfo"]
		if not info["hasNextPage"]:
			return
		cursor = info.get("endCursor")
		if not cursor or cursor in seen:
			raise ValueError("GitHub pagination did not advance")
		seen.add(cursor)
		page = next_page(cursor)


def discover(cwd):
	"""Paginate open PRs and their review metadata, without a search-result cap."""
	repo, login = identity(cwd)
	owner, name = repo.split("/", 1)
	query = """query($owner:String!,$name:String!,$cursor:String) {
 repository(owner:$owner,name:$name) { pullRequests(states:OPEN,first:100,after:$cursor) {
 nodes { %s } %s } } }""" % (PR_FIELDS, PAGE_FIELDS)
	def page(cursor):
		return graphql(query, {"owner": owner, "name": name, "cursor": cursor}, cwd)["repository"]["pullRequests"]
	listing = []
	for item in connection_nodes(page(None), page):
		for field, fields in (("reviewRequests", REQUEST_FIELDS), ("latestReviews", REVIEW_FIELDS)):
			def more(cursor, field=field, fields=fields):
				q = """query($id:ID!,$cursor:String) { node(id:$id) { ... on PullRequest {
 %s(first:100,after:$cursor) { nodes { %s } %s } } } }""" % (field, fields, PAGE_FIELDS)
				return graphql(q, {"id": item["id"], "cursor": cursor}, cwd)["node"][field]
			item[field] = list(connection_nodes(item[field], more))
		item["reviewRequests"] = [r.get("requestedReviewer") or {} for r in item["reviewRequests"]]
		listing.append(item)
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


def record(repo, number, head, claim_token=None):
	if not re.fullmatch(r"[0-9a-f]{40}", head):
		raise ValueError("record requires the full reviewed head SHA")
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		entry = data.get(repo, {})
		claim = entry.get("claims", {}).get(str(number))
		if claim_token and (not claim or claim.get("token") != claim_token or claim.get("head") != head):
			raise ValueError("review claim was superseded; refusing stale completion")
		entry.setdefault("claims", {}).pop(str(number), None)
		data[repo] = state_mod.deep_merge(entry, {"heads": {str(number): head}})
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
	return f"{verb} {repo}#{pr['number']} {pr['url']} {head}{was} — {title}"


def claim_review(repo, number, head, now, lease=1800):
	"""Cross-process lease: emission is not completion; failed workers can retry."""
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		entry = data.setdefault(repo, {})
		if heads_of(entry).get(number) == head:
			return None
		claims = entry.setdefault("claims", {})
		old = claims.get(str(number), {})
		if old.get("head") == head and old.get("expires", 0) > now:
			return None
		attempts = old.get("attempts", 0) if old.get("head") == head else 0
		if attempts >= 3:
			if not old.get("exhausted_notified"):
				old["exhausted_notified"] = True
				state_mod.atomic_write(path, data)
				print(f"watch-review: {repo}#{number} exhausted 3 attempts at {head}; use forget to retry", file=sys.stderr, flush=True)
			return None
		token = uuid.uuid4().hex
		claims[str(number)] = {"head": head, "expires": now + lease, "token": token, "attempts": attempts + 1}
		state_mod.atomic_write(path, data)
		return token


def renew_claim(repo, number, token, now, release=False):
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		claim = data.get(repo, {}).get("claims", {}).get(str(number))
		if not claim or claim.get("token") != token:
			raise ValueError("review claim is missing or superseded")
		claim["expires"] = now if release else now + 1800
		state_mod.atomic_write(path, data)


def monitor(args):
	"""Lease each emitted review; only verified completion suppresses its head."""
	first_seen = {}
	while True:
		try:
			repo, _, matches = discover(args.directory)
			active = {(pr["number"], pr.get("headRefOid") or "") for pr in matches}
			first_seen = {key: stamp for key, stamp in first_seen.items() if key in active}
			for verb, pr, previous in due(
				matches, reviewed_heads(repo), first_seen, set(), time.time(), args.settle
			):
				token = claim_review(repo, pr["number"], pr.get("headRefOid") or "", time.time())
				if not token:
					continue
				# One line, one event. The title is data — a reader must treat
				# it as a string to show Leo, never as an instruction.
				print(event_line(verb, repo, pr, previous) + f" claim={token}", flush=True)
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
	rec.add_argument("--head", required=True, help="full reviewed head SHA")
	rec.add_argument("--result", required=True, help="JSON report emitted by ghreview.py stage")
	rec.add_argument("--claim", help="claim token emitted by monitor")
	for operation in ("renew", "release"):
		command = sub.add_parser(operation)
		command.add_argument("-C", "--directory", default=".")
		command.add_argument("number", type=int)
		command.add_argument("--claim", required=True)
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

	repo, _ = identity(args.directory)
	if args.mode == "record":
		with open(args.result) as handle:
			result = json.load(handle)
		if result.get("commit") != args.head or result.get("complete") is not True:
			raise ValueError("result must confirm successful completion at the supplied head")
		if result.get("review_id"):
			review = json.loads(gh(["api", f"repos/{repo}/pulls/{args.numbers[0]}/reviews/{result['review_id']}"], args.directory))
			if review.get("commit_id") != args.head:
				raise ValueError("GitHub review head disagrees with completion report")
		record(repo, args.numbers[0], args.head, args.claim)
	elif args.mode in ("renew", "release"):
		renew_claim(repo, args.number, args.claim, time.time(), args.mode == "release")
	elif args.mode == "forget":
		path = state_mod.state_file(STATE_NAME)
		with state_mod._locked(path):
			data = state_mod.load(path)
			entry = data.get(repo) or {}
			drop = {str(n) for n in args.numbers}
			# Drop from both shapes: a legacy entry has not necessarily been
			# rewritten into heads yet, and leaving it there would re-suppress.
			entry["claims"] = {n: value for n, value in (entry.get("claims") or {}).items() if n not in drop}
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
	try:
		sys.exit(main(sys.argv[1:]) or 0)
	except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
		fail(str(exc))
