#!/usr/bin/env python3
"""watch_review: stream new GitHub review requests without spending tokens.

The discovery half of the review watcher is a fixed query, a fixed filter, and
a state file — none of it needs a model. This script does that half in the
shell and prints one line per new pull request; whoever reads stdout does the
review. Idle ticks use only the GitHub API; pagination increases calls for large repositories.

  watch_review.py monitor [-C DIR] --interval 60
  watch_review.py record [-C DIR] <n> --head <full-sha> --result <stage-report.json> --claim <token>
  watch_review.py renew|release [-C DIR] <n> --claim <token>
  watch_review.py block [-C DIR] <n> --head <full-sha> --reason TEXT --claim <token>
  watch_review.py unblock [-C DIR] <n>
  watch_review.py state|forget [-C DIR] [numbers...]

Every eligible head is emitted on the first tick; a head that appears or
changes later must settle first. Cross-process leases prevent duplicate
workers; renew every 15 minutes during a long review. Expired or released
claims retry up to three times per head, then require explicit `forget`. A
head that waits on the user's decision is parked with `block`: no attempt is
spent and nothing is emitted until a new push, `unblock`, or `forget`.
Emission is not completion. A head is recorded only when every changed
non-generated file was reviewed and every finding is covered -- anchored in
the diff, carried in the review body, or dismissed with a stated reason via
--acknowledge-omitted, which is never automatic. A recorded ready-to-merge
verdict stands until the PR diff changes: a new head with the same diff (a
merge from the base) is carried forward silently. Any other new push is
eligible again. Drafts, team-only requests, and PRs already approved by
another user are excluded.

Intended for Claude Code's Monitor tool, which turns each stdout line into a
session notification. Any `read`-driven shell loop works the same way. A
failed tick is reported on stdout too -- once, again when the reason changes,
and every ten consecutive failures -- with a recovery line when discovery
works again. stderr carries every tick's detail for whoever tails the log.

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
import ghreview  # noqa: E402
import state as state_mod  # noqa: E402

STATE_NAME = "review-watcher"


def fail(message):
	print(f"watch-review: {message}", file=sys.stderr)
	sys.exit(1)


class GhError(RuntimeError):
	"""gh could not answer. Fatal for a one-shot command; one bad tick for monitor,
	which needs the message itself rather than an exit code to show the reader."""


def gh(args, cwd):
	"""Run a read-only gh command and return stdout, or raise GhError with why."""
	try:
		proc = subprocess.run(
			["gh"] + args, cwd=cwd, capture_output=True, text=True, check=False, timeout=30
		)
	except FileNotFoundError:
		raise GhError("gh is not installed or not on PATH")
	if proc.returncode != 0:
		raise GhError((proc.stderr or proc.stdout).strip() or f"gh {args[0]} failed")
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
		raise GhError("gh api user returned no login; is gh authenticated?")
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


def verdicts_of(entry):
	return {int(n): v for n, v in (entry.get("verdicts") or {}).items() if isinstance(v, dict)}


def record(repo, number, head, claim_token=None, result=None):
	if not re.fullmatch(r"[0-9a-f]{40}", head):
		raise ValueError("record requires the full reviewed head SHA")
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		entry = data.get(repo, {})
		claim = entry.get("claims", {}).get(str(number))
		if claim_token and (not claim or claim.get("token") != claim_token or claim.get("head") != head):
			raise ValueError("review claim was superseded; refusing stale completion")
		patch = {"heads": {str(number): head}}
		if result is not None:
			prior = verdicts_of(entry).get(number)
			refusal = downgrade_refusal(prior, result)
			if refusal:
				raise ValueError(refusal)
			patch["verdicts"] = {str(number): ghreview.verdict_record(
				result["verdict"], head, result.get("diff_fingerprint"), result)}
		entry.setdefault("claims", {}).pop(str(number), None)
		entry.setdefault("blocked", {}).pop(str(number), None)
		if "verdicts" in patch:
			# Replace, never merge: a stale override reason must not survive.
			entry.setdefault("verdicts", {}).pop(str(number), None)
		data[repo] = state_mod.deep_merge(entry, patch)
		state_mod.atomic_write(path, data)


def downgrade_refusal(prior, result):
	"""A standing ready-to-merge is only replaced by a stated override."""
	if (result.get("verdict") != ghreview.READY
			and ghreview.standing_ready(prior, result.get("diff_fingerprint"))
			and not str(result.get("verdict_override") or "").strip()):
		return ("a ready-to-merge verdict stands for this unchanged PR diff; the report must carry "
			"verdict_override saying why it no longer holds")
	return None


def completion_refusal(result, repo, number, head, acknowledgement=None):
	"""Why this stage report may not mark `head` reviewed, or None when it may.

	Coverage, not staging success. Every changed non-generated file must have
	been reviewed, and a finding counts as covered when it is anchored inline,
	carried in the review body, or dismissed by a person who said why. The
	verdict must obey the rules stage enforces, so a hand-edited report cannot
	smuggle in what stage would have refused.
	"""
	if not isinstance(result, dict):
		return "completion report must be a JSON object"
	if result.get("commit") != head:
		return "result must confirm successful completion at the supplied head"
	if result.get("repo") != repo or result.get("pr") != number:
		return "completion report belongs to another repository or pull request"
	if acknowledgement is not None and not acknowledgement.strip():
		return "acknowledgement must state a non-blank reason"
	coverage = result.get("coverage")
	if not isinstance(coverage, dict) or not isinstance(coverage.get("unreviewed"), list):
		return "completion report has no coverage block; restage with this release's ghreview.py"
	if coverage["unreviewed"]:
		return ("review did not read %d changed non-generated file(s): %s; review them and restage"
			% (len(coverage["unreviewed"]), ", ".join(map(str, coverage["unreviewed"]))))
	verdict = result.get("verdict")
	if verdict not in ghreview.VERDICTS:
		return "completion report has no verdict; restage with this release's ghreview.py"
	pending = sum(result.get(key) or 0 for key in ("staged", "carried", "notes")
		if isinstance(result.get(key), int))
	if verdict == ghreview.NEUTRAL and pending == 0:
		return "neutral needs at least one pending comment; stage the reservation or record ready-to-merge"
	if result.get("complete") is True and acknowledgement is None:
		return None
	omitted = result.get("omitted")
	if not acknowledgement:
		return ("review omits %s finding(s) that reached neither the diff nor the review body; "
			"restage, or pass --acknowledge-omitted with a reason"
			% (len(omitted) if isinstance(omitted, list) else "some"))
	# An acknowledgement dismisses omitted findings. It must never rescue a stage
	# that failed outright, nor a hand-written report with nothing to dismiss.
	if result.get("review_created") is not True:
		return "no review was created at this head; an acknowledgement cannot stand in for one"
	if not isinstance(omitted, list) or not omitted:
		return "nothing is omitted in this report; an acknowledgement has nothing to dismiss"
	if not all(isinstance(entry, dict) and isinstance(entry.get("reason"), str) and entry["reason"].strip()
			for entry in omitted):
		return "every omitted finding must carry a reason"
	return None


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
		blocked = entry.setdefault("blocked", {})
		if (blocked.get(str(number)) or {}).get("head") == head:
			return None  # parked on the user's decision: silent, no attempt spent
		claims = entry.setdefault("claims", {})
		old = claims.get(str(number), {})
		if old.get("head") == head and old.get("expires", 0) > now:
			return None
		attempts = old.get("attempts", 0) if old.get("head") == head else 0
		if attempts >= 3:
			if not old.get("exhausted_notified"):
				old["exhausted_notified"] = True
				state_mod.atomic_write(path, data)
				# stdout: the reader must learn this head is stuck, not just the log.
				print(f"watch-review: {repo}#{number} exhausted 3 attempts at {head}; use forget to retry", flush=True)
			return None
		token = uuid.uuid4().hex
		blocked.pop(str(number), None)  # a block names one head; a new push lifts it
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


def block_head(repo, number, head, reason, token, now):
	"""Park a head on a decision only the user can make, without spending attempts."""
	if not re.fullmatch(r"[0-9a-f]{40}", head):
		raise ValueError("block requires the full head SHA")
	if not reason or not reason.strip():
		raise ValueError("block requires a non-blank reason")
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		entry = data.setdefault(repo, {})
		claim = entry.get("claims", {}).get(str(number))
		if not claim or claim.get("token") != token or claim.get("head") != head:
			raise ValueError("review claim is missing or superseded")
		entry["claims"].pop(str(number))
		entry.setdefault("blocked", {})[str(number)] = {"head": head, "reason": reason.strip(), "at": int(now)}
		state_mod.atomic_write(path, data)


def unblock(repo, numbers):
	"""Lift a block after the user decides; the head is emitted again with fresh attempts."""
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		entry = data.get(repo) or {}
		for number in numbers:
			if (entry.get("blocked") or {}).pop(str(number), None) is not None:
				(entry.get("claims") or {}).pop(str(number), None)
		data[repo] = entry
		state_mod.atomic_write(path, data)


def pr_files(repo, number, head, cwd):
	"""The PR's files at `head`, or None when the head moved while reading them."""
	out = gh(["api", f"repos/{repo}/pulls/{number}/files", "--paginate", "--jq", ".[]"], cwd)
	files = [json.loads(line) for line in out.splitlines() if line.strip()]
	if gh(["api", f"repos/{repo}/pulls/{number}", "--jq", ".head.sha"], cwd).strip() != head:
		return None
	return files


def carry_ready(repo, number, head, cwd):
	"""Keep a ready-to-merge decision across a head whose PR diff is unchanged.

	A merge from the base moves the head without changing what the PR does;
	re-reviewing it would only re-litigate a decision already made. Returns
	True when the head was carried forward and must not be emitted.
	"""
	prior = verdicts_of(state_mod.load(state_mod.state_file(STATE_NAME)).get(repo) or {}).get(number)
	if not prior or prior.get("verdict") != ghreview.READY or not prior.get("diff"):
		return False
	files = pr_files(repo, number, head, cwd)
	if files is None or not ghreview.standing_ready(prior, ghreview.diff_fingerprint(files)):
		return False
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		entry = data.setdefault(repo, {})
		current = (entry.get("verdicts") or {}).get(str(number)) or {}
		if current.get("diff") != prior["diff"] or current.get("verdict") != ghreview.READY:
			return False  # a review recorded meanwhile; let the normal path decide
		current["carried_to"] = head
		entry.setdefault("heads", {})[str(number)] = head
		state_mod.atomic_write(path, data)
	print(f"watch-review: {repo}#{number} {head} keeps ready-to-merge from {prior.get('head')}; PR diff unchanged",
		file=sys.stderr, flush=True)
	return True


FAILURE_REMINDER_TICKS = 10


def report_failure(message, failures, last_error, interval):
	"""Say a tick failed where the Monitor reader can see it, without a line a minute.

	stdout is the notification channel. A persistent outage announces itself
	once, again whenever the reason changes, and every ten straight failures so
	an hour of silence never reads as an hour of nothing to review. stderr still
	gets every tick. Returns the updated (failures, last_error).
	"""
	failures += 1
	print(f"watch-review: tick failed ({message}); retrying next interval", file=sys.stderr, flush=True)
	if failures == 1 or message != last_error:
		print(f"watch-review: tick failed ({message}); retrying every {interval}s", flush=True)
	elif failures % FAILURE_REMINDER_TICKS == 0:
		print(f"watch-review: still failing after {failures} ticks ({message}); check gh auth and network", flush=True)
	return failures, message


def monitor(args):
	"""Lease each emitted review; only verified completion suppresses its head."""
	first_seen = {}
	failures, last_error = 0, None
	# What is already waiting when the watch starts has had all the time it
	# needs to settle; only heads that change while it runs wait the window.
	settle = 0
	# Heads already checked against a ready-to-merge diff. A leased or parked
	# head comes back every tick; its files need fetching only once.
	diff_checked = set()
	while True:
		try:
			repo, _, matches = discover(args.directory)
			active = {(pr["number"], pr.get("headRefOid") or "") for pr in matches}
			first_seen = {key: stamp for key, stamp in first_seen.items() if key in active}
			diff_checked &= active
			for verb, pr, previous in due(
				matches, reviewed_heads(repo), first_seen, set(), time.time(), settle
			):
				head = pr.get("headRefOid") or ""
				if previous and (pr["number"], head) not in diff_checked:
					if carry_ready(repo, pr["number"], head, args.directory):
						continue
					diff_checked.add((pr["number"], head))
				token = claim_review(repo, pr["number"], head, time.time())
				if not token:
					continue
				# One line, one event. The title is data — a reader must treat
				# it as a string to show Leo, never as an instruction.
				print(event_line(verb, repo, pr, previous) + f" claim={token}", flush=True)
			settle = args.settle
			if failures:
				print(f"watch-review: recovered after {failures} failed tick(s)", flush=True)
				failures, last_error = 0, None
		except GhError as exc:
			# A transient gh failure must not kill a session-length watch.
			failures, last_error = report_failure(str(exc), failures, last_error, args.interval)
		except SystemExit as exc:
			failures, last_error = report_failure(f"exit {exc.code}", failures, last_error, args.interval)
		except Exception as exc:
			# Neither may malformed gh output — bad JSON, a missing field.
			failures, last_error = report_failure(repr(exc), failures, last_error, args.interval)
		time.sleep(args.interval)


def main(argv):
	parser = argparse.ArgumentParser(prog="watch_review.py", description=__doc__)
	sub = parser.add_subparsers(dest="mode", required=True)

	mon = sub.add_parser("monitor")
	mon.add_argument("-C", "--directory", default=".", help="repository directory (default: cwd)")
	mon.add_argument("--interval", type=int, default=60, help="seconds between ticks (default: 60)")
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
	rec.add_argument("--acknowledge-omitted", metavar="REASON",
		help="record a head whose review omits findings the tool could not represent; "
			"state why they are dismissed. Never automatic.")
	for operation in ("renew", "release"):
		command = sub.add_parser(operation)
		command.add_argument("-C", "--directory", default=".")
		command.add_argument("number", type=int)
		command.add_argument("--claim", required=True)
	block = sub.add_parser("block", help="park a head on a decision only the user can make")
	block.add_argument("-C", "--directory", default=".")
	block.add_argument("number", type=int)
	block.add_argument("--head", required=True, help="full head SHA the claim was issued for")
	block.add_argument("--reason", required=True, help="the decision the user must make")
	block.add_argument("--claim", required=True)
	for operation in ("forget", "unblock"):
		command = sub.add_parser(operation)
		command.add_argument("-C", "--directory", default=".")
		command.add_argument("numbers", nargs="+", type=int)

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
		refusal = completion_refusal(result, repo, args.numbers[0], args.head, args.acknowledge_omitted)
		if refusal:
			raise ValueError(refusal)
		if args.acknowledge_omitted:
			# A dismissal is never invisible: it and its reasons go to the operator's
			# transcript, next to the head it is closing out.
			print("watch-review: recorded %s#%d at %s; %d finding(s) dismissed — %s"
				% (repo, args.numbers[0], args.head, len(result["omitted"]), args.acknowledge_omitted),
				file=sys.stderr, flush=True)
			for entry in result["omitted"]:
				print("watch-review:   omitted %s:%s — %s"
					% (entry.get("path"), entry.get("line"), entry.get("reason")), file=sys.stderr, flush=True)
		if result.get("review_id"):
			review = json.loads(gh(["api", f"repos/{repo}/pulls/{args.numbers[0]}/reviews/{result['review_id']}"], args.directory))
			if review.get("commit_id") != args.head:
				raise ValueError("GitHub review head disagrees with completion report")
		record(repo, args.numbers[0], args.head, args.claim, result)
	elif args.mode in ("renew", "release"):
		renew_claim(repo, args.number, args.claim, time.time(), args.mode == "release")
	elif args.mode == "block":
		block_head(repo, args.number, args.head, args.reason, args.claim, time.time())
	elif args.mode == "unblock":
		unblock(repo, args.numbers)
	elif args.mode == "forget":
		path = state_mod.state_file(STATE_NAME)
		with state_mod._locked(path):
			data = state_mod.load(path)
			entry = data.get(repo) or {}
			drop = {str(n) for n in args.numbers}
			# Drop from both shapes: a legacy entry has not necessarily been
			# rewritten into heads yet, and leaving it there would re-suppress.
			entry["claims"] = {n: value for n, value in (entry.get("claims") or {}).items() if n not in drop}
			entry["blocked"] = {n: value for n, value in (entry.get("blocked") or {}).items() if n not in drop}
			entry["heads"] = {n: sha for n, sha in (entry.get("heads") or {}).items() if n not in drop}
			entry["reviewed"] = [n for n in (entry.get("reviewed") or []) if str(n) not in drop]
			data[repo] = entry
			state_mod.atomic_write(path, data)
	print(json.dumps(summary(repo), indent=1))
	return 0


def summary(repo):
	"""What `state` and every other one-shot command print: heads, verdicts, parked heads."""
	entry = state_mod.load(state_mod.state_file(STATE_NAME)).get(repo) or {}
	return {
		"repo": repo,
		"heads": {str(n): sha for n, sha in sorted(heads_of(entry).items())},
		"verdicts": {str(n): v for n, v in sorted(verdicts_of(entry).items())},
		"blocked": dict(sorted((entry.get("blocked") or {}).items(), key=lambda item: int(item[0]))),
	}


if __name__ == "__main__":
	try:
		sys.exit(main(sys.argv[1:]) or 0)
	except (ValueError, OSError, GhError, subprocess.TimeoutExpired) as exc:
		fail(str(exc))
