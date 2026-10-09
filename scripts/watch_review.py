#!/usr/bin/env python3
"""watch_review: stream new GitHub review requests without spending tokens.

The discovery half of the review watcher is a fixed query, a fixed filter, and
a state file — none of it needs a model. This script does that half in the
shell and prints one line per new pull request; whoever reads stdout does the
review. Idle ticks use only the GitHub API: one search for open pull requests
requesting the user's review, whatever the size of the repository.

  watch_review.py monitor [-C DIR] --interval 60
  watch_review.py start [-C DIR] <n> --head <full-sha> --claim <token>
  watch_review.py renew|release [-C DIR] <n> --claim <token>
  watch_review.py record [-C DIR] <n> --head <full-sha> --result <stage-report.json> --claim <token>
  watch_review.py block [-C DIR] <n> --head <full-sha> --reason TEXT --claim <token>
  watch_review.py unblock [-C DIR] <n>
  watch_review.py state|forget [-C DIR] [numbers...]

Each event is one line, machine fields first and the attacker-written title
last:

  review-requested OWNER/REPO#N head=SHA claim=TOKEN URL — TITLE
  re-review OWNER/REPO#N head=SHA prev=SHA claim=TOKEN URL — TITLE

`prev` is the full head reviewed last, absent when it is unknown.

Every eligible head is emitted on the first tick; a head that appears or
changes later must settle first. Cross-process claims prevent duplicate
workers. An emitted claim is queued: the running monitor holds it however
long the reader's backlog is, so it never expires before work starts. `start`
checks the claim and begins a 30-minute lease; the monitor keeps a started
lease alive for up to two hours, because the reader is blocked on a
foreground reviewer and cannot renew, and `renew` extends it. When the monitor
stops, its claims lapse within a few intervals and another watcher may take
them; a lapsed claim that never started spends no attempt. A head gets three
attempts in total (two retries): each start, or a release before one, spends
one. After the third, the head is reported exhausted once and requires
explicit `forget`. A head that waits on the user's decision is parked with
`block`: no attempt is spent and nothing is emitted until a new push,
`unblock`, or `forget`.

Emission is not completion. A head is recorded only when every changed
non-generated file was reviewed and every finding is covered -- anchored in
the diff, carried in the review body, or dismissed with a stated reason via
--acknowledge-omitted, which is never automatic. A recorded ready-to-merge
verdict, whether the watcher or a manual review-pr pass staged it, stands
until the PR diff changes: its own head is never emitted, and a new head with
the same diff (a merge from the base) is carried forward silently. Any other
new push is eligible again. Drafts, team-only requests, and PRs already
approved by another user are excluded.

Intended for Claude Code's Monitor tool, which turns each stdout line into a
session notification. Any `read`-driven shell loop works the same way. A
failed tick is reported on stdout too -- once, again when the reason changes,
and every ten consecutive failures -- with a recovery line when discovery
works again. stderr is the log: every tick's detail, carried verdicts, and
pruning. Never merge it into stdout, or idle ticks wake the reader.

State lives in the review-watcher state file managed by state.py, keyed by
"owner/repo" — the same file and shape the watch-review skill reads. Entries
written before heads were tracked carry a bare list of numbers; those migrate on
read to an unknown head, so each comes back once and then tracks properly.
Pull requests closed or merged more than 30 days ago are pruned from it.
"""
import argparse
import calendar
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghreview  # noqa: E402
import state as state_mod  # noqa: E402

STATE_NAME = "review-watcher"
SHA_RE = r"[0-9a-f]{40}"
TOKEN_RE = r"[0-9a-f]{32}"  # uuid4().hex, as claim_review mints it
LEASE = 1800  # seconds a started review holds its claim after `start` or `renew`
MAX_HELD = 4 * LEASE  # how long a running monitor keeps a started lease alive
ATTEMPTS = 3  # started (or released) claims per head: the first try and two retries
CLOSED_RETENTION = 30 * 86400
PRUNE_EVERY = 6 * 3600
PRUNE_BATCH = 50
# A files listing that never matches its head is reviewed in full, not dropped.
UNSTABLE_TRIES = 3
CARRIED, UNSTABLE = "carried", "unstable"
SERIOUS = "seriously-problematic"


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
	"""The pull requests in a discovery payload that are worth reviewing.

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
# Fifty results with two nested connections each keep a page at one GraphQL point.
SEARCH_QUERY = """query($q:String!,$cursor:String) {
 search(type:ISSUE,query:$q,first:50,after:$cursor) {
 nodes { ... on PullRequest { %s } } %s } }""" % (PR_FIELDS, PAGE_FIELDS)

_IDENTITY = {}
_CANONICAL = {}


def identity(cwd):
	"""(owner/repo as GitHub spells it, login), asked once per directory per process.

	A watch runs for a whole session; asking both every tick cost two calls a
	minute for answers that do not change.
	"""
	key = os.path.realpath(cwd)
	if key not in _IDENTITY:
		repo = json.loads(gh(["repo", "view", "--json", "nameWithOwner"], cwd))["nameWithOwner"]
		login = gh(["api", "user", "--jq", ".login"], cwd).strip()
		if not login:
			raise GhError("gh api user returned no login; is gh authenticated?")
		_IDENTITY[key] = (repo, login)
	return _IDENTITY[key]


def canonical_repo(name, cwd):
	"""GitHub's spelling of owner/repo. Both halves are case-insensitive and a
	renamed repository redirects, so one repository has many spellings."""
	if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
		return None
	if name not in _CANONICAL:
		_CANONICAL[name] = gh(["api", f"repos/{name}", "--jq", ".full_name"], cwd).strip() or None
	return _CANONICAL[name]


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


def search_query(repo):
	# `user-review-requested:@me` is GitHub's qualifier for direct requests only.
	# `review-requested:` would also match every team request, which CODEOWNERS
	# can put on most pull requests of a large repository. eligible() still
	# checks the live review requests, not the search index.
	return f"repo:{repo} is:pr is:open draft:false user-review-requested:@me"


def discover(cwd):
	"""Open pull requests that request the user's review, from one search.

	Paging every open pull request cost GraphQL points in proportion to the
	repository, every minute. The search costs the same on any repository; the
	fields come from the live objects, so only membership can lag the index.
	"""
	repo, login = identity(cwd)
	q = search_query(repo)

	def page(cursor):
		return graphql(SEARCH_QUERY, {"q": q, "cursor": cursor}, cwd)["search"]

	listing = []
	for item in connection_nodes(page(None), page):
		if not isinstance(item, dict) or "number" not in item:
			continue  # a result that is not a pull request has no fields here
		for field, fields in (("reviewRequests", REQUEST_FIELDS), ("latestReviews", REVIEW_FIELDS)):
			def more(cursor, field=field, fields=fields):
				q = """query($id:ID!,$cursor:String) { node(id:$id) { ... on PullRequest {
 %s(first:100,after:$cursor) { nodes { %s } %s } } } }""" % (field, fields, PAGE_FIELDS)
				return graphql(q, {"id": item["id"], "cursor": cursor}, cwd)["node"][field]
			item[field] = list(connection_nodes(item[field], more))
		item["reviewRequests"] = [r.get("requestedReviewer") or {} for r in item["reviewRequests"]]
		listing.append(item)
	return repo, login, eligible(listing, login)


def load_entry(repo):
	return state_mod.load(state_mod.state_file(STATE_NAME)).get(repo) or {}


def reviewed_heads(repo):
	"""{pull request number: reviewed head sha}. "" means "reviewed, head unknown"."""
	return heads_of(load_entry(repo))


def heads_of(entry):
	heads = {int(n): sha for n, sha in (entry.get("heads") or {}).items()}
	# Pre-heads state was a bare list of numbers. Treat those as reviewed at an
	# unknown head: each returns once, records a real head, and tracks from there.
	for number in entry.get("reviewed") or []:
		heads.setdefault(int(number), "")
	return heads


def verdicts_of(entry):
	return {int(n): v for n, v in (entry.get("verdicts") or {}).items() if isinstance(v, dict)}


def ready_heads(entry):
	"""{number: heads a recorded ready-to-merge covers}: its own head and the one it was carried to.

	ghreview.py stage writes this verdict for a manual review-pr pass as well as
	for the watcher, and refuses ready-to-merge unless every changed file was
	reviewed, so such a head is as reviewed as one the watcher recorded.
	"""
	covered = {}
	for number, verdict in verdicts_of(entry).items():
		if verdict.get("verdict") != ghreview.READY:
			continue
		heads = {h for h in (verdict.get("head"), verdict.get("carried_to"))
			if isinstance(h, str) and re.fullmatch(SHA_RE, h)}
		if heads:
			covered[number] = heads
	return covered


def known_heads(entry):
	"""heads_of, plus the latest ready-to-merge head where the watcher recorded none."""
	known = heads_of(entry)
	for number, verdict in verdicts_of(entry).items():
		if number in known or verdict.get("verdict") != ghreview.READY:
			continue
		head = verdict.get("carried_to") or verdict.get("head")
		if isinstance(head, str) and re.fullmatch(SHA_RE, head):
			known[number] = head
	return known


def record(repo, number, head, claim_token=None, result=None):
	if not re.fullmatch(SHA_RE, head):
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


def with_canonical_repo(result, repo, cwd):
	"""The report under GitHub's spelling of its repository, when that is `repo`.

	The report names whatever -R the reviewer passed, so Foo/Bar and foo/bar
	are one repository; anything GitHub does not resolve to `repo` is left alone
	for completion_refusal to reject.
	"""
	named = result.get("repo") if isinstance(result, dict) else None
	if not isinstance(named, str) or named == repo:
		return result
	try:
		canonical = canonical_repo(named, cwd)
	except GhError:
		canonical = None
	return dict(result, repo=repo) if canonical == repo else result


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
	# As stage enforces: a reservation or a blocking issue the author never sees is not a verdict.
	if verdict == ghreview.NEUTRAL and pending == 0:
		return "neutral needs at least one pending comment; stage the reservation or record ready-to-merge"
	if verdict == SERIOUS and pending == 0:
		return "seriously-problematic needs at least one pending comment or note stating the blocking issue"
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


def due(matches, known, first_seen, emitted, now, settle, covered=None):
	"""Which pull requests to emit this tick, as (verb, pr, previous head).

	`first_seen` is mutated: a head that has just appeared is stamped and held
	until it has stood still for `settle` seconds, so a push burst costs one
	review rather than one per commit. `covered` maps a number to heads a
	standing ready-to-merge already decided. Pure otherwise, so the emit
	decision is testable without a clock or a network.
	"""
	covered = covered or {}
	out = []
	for pr in matches:
		number, head = pr["number"], pr.get("headRefOid") or ""
		key = (number, head)
		if known.get(number) == head or head in covered.get(number, ()) or key in emitted:
			continue
		stamp = first_seen.setdefault(key, now)
		if now - stamp < settle:
			continue
		previous = known.get(number)
		out.append(("re-review" if previous is not None else "review-requested", pr, previous or ""))
	return out


def clean_title(text):
	"""The title as one line of visible text.

	It is attacker-written. Anything Python, a terminal, or a renderer treats
	as a line break (C0 and C1 controls, U+2028, U+2029) becomes a space;
	invisible format characters, the bidi overrides among them, are dropped.
	A field name such as `claim=` loses its `=`, so even a reader that takes
	the last match on the line finds only the real field.
	"""
	out = []
	for ch in text:
		category = unicodedata.category(ch)
		if category in ("Cc", "Zl", "Zp"):
			out.append(" ")
		elif category != "Cf":
			out.append(ch)
	title = re.sub(r"\s+", " ", "".join(out)).strip()
	return re.sub(r"(?i)\b(claim|head|prev)=", r"\1:", title)


def event_line(verb, repo, pr, previous, token=""):
	"""The printed line for one event — always exactly one printable line.

	Every field a reader acts on comes before the title, so a title that
	imitates `claim=` or a second event can only ever follow the real ones.
	"""
	fields = [f"{verb} {repo}#{pr['number']}", "head=" + (pr.get("headRefOid") or "")]
	if previous:
		fields.append("prev=" + previous)
	if token:
		fields.append("claim=" + token)
	fields.append(pr["url"])
	return " ".join(fields) + " — " + clean_title(pr.get("title") or "")


def claim_review(repo, number, head, now, monitor="", hold=LEASE):
	"""Queue a cross-process claim for one head: emission is not completion.

	The claim is held for `hold` seconds, which the emitting monitor renews
	every tick (hold_claims), so it waits out any backlog without starting its
	lease. Attempts count started work, not emissions.
	"""
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		entry = data.setdefault(repo, {})
		if heads_of(entry).get(number) == head or head in ready_heads(entry).get(number, ()):
			return None
		blocked = entry.setdefault("blocked", {})
		if (blocked.get(str(number)) or {}).get("head") == head:
			return None  # parked on the user's decision: silent, no attempt spent
		claims = entry.setdefault("claims", {})
		old = claims.get(str(number), {})
		if old.get("head") == head and old.get("expires", 0) > now:
			return None
		attempts = old.get("attempts", 0) if old.get("head") == head else 0
		if attempts >= ATTEMPTS:
			if not old.get("exhausted_notified"):
				old["exhausted_notified"] = True
				state_mod.atomic_write(path, data)
				# stdout: the reader must learn this head is stuck, not just the log.
				print(f"watch-review: {repo}#{number} exhausted {ATTEMPTS} attempts at {head}; use forget to retry",
					flush=True)
			return None
		token = uuid.uuid4().hex
		blocked.pop(str(number), None)  # a block names one head; a new push lifts it
		claims[str(number)] = {"head": head, "token": token, "attempts": attempts, "state": "queued",
			"monitor": monitor, "expires": now + hold}
		state_mod.atomic_write(path, data)
		return token


def current_claim(entry, number, token, head=None):
	claim = (entry.get("claims") or {}).get(str(number))
	if not claim or claim.get("token") != token or (head is not None and claim.get("head") != head):
		raise ValueError("review claim is missing or superseded")
	return claim


def start_claim(repo, number, token, now, head=None, lease=LEASE):
	"""Begin, or extend, the lease of a claim that is still current.

	The first start spends one attempt. A claim written before claims queued
	has no state; it spent its attempt when it was emitted.
	"""
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		claim = current_claim(data.get(repo) or {}, number, token, head)
		state = claim.get("state", "started")
		if state == "released":
			raise ValueError("review claim was released; wait for the head to be emitted again")
		if state == "queued":
			claim["attempts"] = claim.get("attempts", 0) + 1
		claim.update(state="started", touched=now, expires=now + lease)
		state_mod.atomic_write(path, data)
		return dict(claim)


def release_claim(repo, number, token, now):
	"""Give the head back for a retry. Releasing before `start` still spends an
	attempt, so a reader that keeps declining a head cannot loop on it."""
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		claim = current_claim(data.get(repo) or {}, number, token)
		if claim.get("state") == "queued":
			claim["attempts"] = claim.get("attempts", 0) + 1
		claim.update(state="released", expires=now)
		state_mod.atomic_write(path, data)
		return dict(claim)


def renew_claim(repo, number, token, now, release=False):
	return release_claim(repo, number, token, now) if release else start_claim(repo, number, token, now)


def hold_claims(repo, monitor, now, hold):
	"""Keep this monitor's claims alive while it runs.

	A queued claim waits behind other reviews in the reader's queue and must not
	expire before work starts. A started one is a review in this same session,
	whose reader is blocked on a foreground reviewer and cannot renew; it is
	held until MAX_HELD after its last start or renew, so an abandoned review
	still comes back. Once the monitor stops, both lapse within `hold`.
	"""
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		changed = False
		for claim in ((data.get(repo) or {}).get("claims") or {}).values():
			if not isinstance(claim, dict) or not monitor or claim.get("monitor") != monitor:
				continue
			state = claim.get("state")
			if state == "started" and now - claim.get("touched", 0) >= MAX_HELD:
				continue
			# Rewrite at most every half hold, not every tick.
			if state in ("queued", "started") and claim.get("expires", 0) < now + hold / 2:
				claim["expires"] = now + hold
				changed = True
		if changed:
			state_mod.atomic_write(path, data)


def block_head(repo, number, head, reason, token, now):
	"""Park a head on a decision only the user can make, without spending attempts."""
	if not re.fullmatch(SHA_RE, head):
		raise ValueError("block requires the full head SHA")
	if not reason or not reason.strip():
		raise ValueError("block requires a non-blank reason")
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		entry = data.setdefault(repo, {})
		current_claim(entry, number, token, head)
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


def drop_numbers(repo, numbers, verdicts=False):
	"""Forget pull requests: reviewed heads, leases and attempts, blocks, and optionally verdicts."""
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		entry = data.get(repo) or {}
		drop = {str(n) for n in numbers}
		# Drop from both shapes: a legacy entry has not necessarily been
		# rewritten into heads yet, and leaving it there would re-suppress.
		for key in ("claims", "blocked", "heads") + (("verdicts",) if verdicts else ()):
			entry[key] = {n: value for n, value in (entry.get(key) or {}).items() if n not in drop}
		entry["reviewed"] = [n for n in (entry.get("reviewed") or []) if str(n) not in drop]
		data[repo] = entry
		state_mod.atomic_write(path, data)


def tracked_numbers(entry):
	numbers = set()
	for key in ("heads", "verdicts", "claims", "blocked"):
		numbers.update(int(n) for n in (entry.get(key) or {}))
	numbers.update(int(n) for n in entry.get("reviewed") or [])
	return numbers


def pr_closed_at(repo, number, cwd):
	"""When the pull request closed or merged, as epoch seconds; None while it is open."""
	state, closed = json.loads(gh(["api", f"repos/{repo}/pulls/{number}", "--jq", "[.state, .closed_at]"], cwd))
	if state != "closed" or not closed:
		return None
	stamp = re.fullmatch(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)Z", closed)
	if not stamp:
		raise ValueError(f"unexpected closed_at {closed!r}")
	return calendar.timegm(tuple(int(part) for part in stamp.groups()))


def prune_closed(repo, cwd, now, skip=(), after=0):
	"""Drop state for pull requests closed or merged more than CLOSED_RETENTION ago.

	The state file otherwise grows for the life of the repository. Retention
	keeps a reopened pull request's verdict for a while. An open pull request
	is never touched, whether or not it still requests a review; neither is
	one GitHub cannot answer for. One run checks at most PRUNE_BATCH, starting
	after `after` and wrapping, so every tracked number is reached in turn.
	Returns (numbers dropped, last number checked).
	"""
	tracked = sorted(tracked_numbers(load_entry(repo)) - set(skip))
	numbers = ([n for n in tracked if n > after] + [n for n in tracked if n <= after])[:PRUNE_BATCH]
	stale = []
	for number in numbers:
		try:
			closed = pr_closed_at(repo, number, cwd)
		except (GhError, ValueError, TypeError):
			continue
		if closed is not None and now - closed > CLOSED_RETENTION:
			stale.append(number)
	if stale:
		drop_numbers(repo, stale, verdicts=True)
		print("watch-review: pruned %s closed over %d days" % (
			", ".join(f"{repo}#{n}" for n in stale), CLOSED_RETENTION // 86400), file=sys.stderr, flush=True)
	return stale, (numbers[-1] if numbers else 0)


def listing_commits(files):
	"""The commits a files listing says it was read from.

	Every file still present at the head names that head in contents_url
	(`?ref=<sha>`); a removed file names the base side and says nothing.
	"""
	commits = set()
	for f in files:
		if f.get("status") == "removed":
			continue
		match = re.search(r"[?&]ref=([0-9a-f]{40})(?:&|$)", f.get("contents_url") or "")
		if match:
			commits.add(match.group(1))
	return commits


def pr_files(repo, number, head, cwd):
	"""The PR's files at `head`, or None when the listing cannot be trusted to be of `head`.

	The head is checked after the read, so a push during it is caught. Shortly
	after a push GitHub can also serve a listing computed for the previous head
	while the pull request already reports the new one; that listing names the
	previous commit.
	"""
	out = gh(["api", f"repos/{repo}/pulls/{number}/files", "--paginate", "--jq", ".[]"], cwd)
	# One object per "\n". jq leaves U+2028 and U+0085 unescaped inside strings,
	# and str.splitlines() would cut an object in two at either.
	files = [json.loads(line) for line in out.split("\n") if line.strip()]
	if gh(["api", f"repos/{repo}/pulls/{number}", "--jq", ".head.sha"], cwd).strip() != head:
		return None
	if listing_commits(files) - {head}:
		return None
	return files


def carry_ready(repo, number, head, cwd):
	"""Keep a ready-to-merge decision across a head whose PR diff is unchanged.

	A merge from the base moves the head without changing what the PR does;
	re-reviewing it would only re-litigate a decision already made. Returns
	CARRIED when the head was carried forward and must not be emitted, UNSTABLE
	when the files could not be read at this head (try again next tick), or
	None when the head needs a review.
	"""
	prior = verdicts_of(load_entry(repo)).get(number)
	if not prior or prior.get("verdict") != ghreview.READY or not prior.get("diff"):
		return None
	files = pr_files(repo, number, head, cwd)
	if files is None:
		return UNSTABLE
	if not ghreview.standing_ready(prior, ghreview.diff_fingerprint(files)):
		return None
	path = state_mod.state_file(STATE_NAME)
	with state_mod._locked(path):
		data = state_mod.load(path)
		entry = data.setdefault(repo, {})
		current = (entry.get("verdicts") or {}).get(str(number)) or {}
		if current.get("diff") != prior["diff"] or current.get("verdict") != ghreview.READY:
			return None  # a review recorded meanwhile; let the normal path decide
		current["carried_to"] = head
		entry.setdefault("heads", {})[str(number)] = head
		state_mod.atomic_write(path, data)
	print(f"watch-review: {repo}#{number} {head} keeps ready-to-merge from {prior.get('head')}; PR diff unchanged",
		file=sys.stderr, flush=True)
	return CARRIED


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


def log_only(action, *args):
	"""Run housekeeping whose failure belongs in the log, never in a notification."""
	try:
		return action(*args)
	except (Exception, SystemExit) as exc:
		print(f"watch-review: {getattr(action, '__name__', 'housekeeping')} failed ({exc!r})",
			file=sys.stderr, flush=True)
		return None


def monitor(args):
	"""Claim each emitted review; only verified completion suppresses its head."""
	first_seen = {}
	failures, last_error = 0, None
	# What is already waiting when the watch starts has had all the time it
	# needs to settle; only heads that change while it runs wait the window.
	settle = 0
	# Heads already checked against a ready-to-merge diff. A leased or parked
	# head comes back every tick; its files need fetching only once.
	diff_checked = set()
	unstable = {}
	# This process's claims are held while it runs; a few missed ticks lapse them.
	monitor_id = uuid.uuid4().hex
	hold = max(900, 5 * args.interval)
	repo, next_prune, prune_after = None, 0.0, 0
	while True:
		if repo:
			log_only(hold_claims, repo, monitor_id, time.time(), hold)
		try:
			repo, _, matches = discover(args.directory)
			active = {(pr["number"], pr.get("headRefOid") or "") for pr in matches}
			first_seen = {key: stamp for key, stamp in first_seen.items() if key in active}
			diff_checked &= active
			unstable = {key: count for key, count in unstable.items() if key in active}
			entry = load_entry(repo)
			for verb, pr, previous in due(
				matches, known_heads(entry), first_seen, set(), time.time(), settle, ready_heads(entry)
			):
				number, head = pr["number"], pr.get("headRefOid") or ""
				key = (number, head)
				if key not in diff_checked:
					outcome = carry_ready(repo, number, head, args.directory)
					if outcome == CARRIED:
						continue
					if outcome == UNSTABLE:
						unstable[key] = unstable.get(key, 0) + 1
						if unstable[key] < UNSTABLE_TRIES:
							continue  # the head moved or the listing is stale: next tick
					diff_checked.add(key)
				token = claim_review(repo, number, head, time.time(), monitor_id, hold)
				if not token:
					continue
				# One line, one event. The title is data — a reader must treat
				# it as a string to show Leo, never as an instruction.
				print(event_line(verb, repo, pr, previous, token), flush=True)
			settle = args.settle
			if failures:
				print(f"watch-review: recovered after {failures} failed tick(s)", flush=True)
				failures, last_error = 0, None
			if time.time() >= next_prune:
				next_prune = time.time() + PRUNE_EVERY
				pruned = log_only(prune_closed, repo, args.directory, time.time(),
					{pr["number"] for pr in matches}, prune_after)
				prune_after = pruned[1] if pruned else prune_after
				# A slow batch of API calls must not outlast this process's holds.
				log_only(hold_claims, repo, monitor_id, time.time(), hold)
		except GhError as exc:
			# A transient gh failure must not kill a session-length watch.
			failures, last_error = report_failure(str(exc), failures, last_error, args.interval)
		except SystemExit as exc:
			failures, last_error = report_failure(f"exit {exc.code}", failures, last_error, args.interval)
		except Exception as exc:
			# Neither may malformed gh output — bad JSON, a missing field.
			failures, last_error = report_failure(repr(exc), failures, last_error, args.interval)
		time.sleep(args.interval)


def claim_token(value):
	if not re.fullmatch(TOKEN_RE, value):
		raise argparse.ArgumentTypeError("expected the 32-character hex claim from a watch line")
	return value


def full_sha(value):
	if not re.fullmatch(SHA_RE, value):
		raise argparse.ArgumentTypeError("expected a full 40-character head SHA")
	return value


def claim_view(repo, number, claim):
	return {"repo": repo, "pr": number, "head": claim.get("head"), "state": claim.get("state"),
		"attempt": claim.get("attempts", 0), "of": ATTEMPTS, "expires": int(claim.get("expires", 0))}


def main(argv):
	parser = argparse.ArgumentParser(prog="watch_review.py", description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
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
	rec.add_argument("--head", required=True, type=full_sha, help="full reviewed head SHA")
	rec.add_argument("--result", required=True, help="JSON report emitted by ghreview.py stage")
	rec.add_argument("--claim", type=claim_token, help="claim token emitted by monitor")
	rec.add_argument("--acknowledge-omitted", metavar="REASON",
		help="record a head whose review omits findings the tool could not represent; "
			"state why they are dismissed. Never automatic.")
	for operation, helptext in (("start", "begin the lease before reviewing; refuses a superseded claim"),
			("renew", "extend a started lease"), ("release", "give the head back for a retry")):
		command = sub.add_parser(operation, help=helptext)
		command.add_argument("-C", "--directory", default=".")
		command.add_argument("number", type=int)
		command.add_argument("--claim", required=True, type=claim_token)
		if operation != "release":
			command.add_argument("--head", required=operation == "start", type=full_sha,
				help="full head SHA the claim was emitted for")
	block = sub.add_parser("block", help="park a head on a decision only the user can make")
	block.add_argument("-C", "--directory", default=".")
	block.add_argument("number", type=int)
	block.add_argument("--head", required=True, type=full_sha, help="full head SHA the claim was issued for")
	block.add_argument("--reason", required=True, help="the decision the user must make")
	block.add_argument("--claim", required=True, type=claim_token)
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
	if args.mode in ("start", "renew", "release"):
		if args.mode == "release":
			claim = release_claim(repo, args.number, args.claim, time.time())
		else:
			claim = start_claim(repo, args.number, args.claim, time.time(), args.head)
		print(json.dumps(claim_view(repo, args.number, claim)))
		return 0
	if args.mode == "record":
		with open(args.result) as handle:
			result = json.load(handle)
		result = with_canonical_repo(result, repo, args.directory)
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
	elif args.mode == "block":
		block_head(repo, args.number, args.head, args.reason, args.claim, time.time())
	elif args.mode == "unblock":
		unblock(repo, args.numbers)
	elif args.mode == "forget":
		drop_numbers(repo, args.numbers)
	print(json.dumps(summary(repo), indent=1))
	return 0


def summary(repo):
	"""What `state` and every other one-shot command print: heads, verdicts, parked heads."""
	entry = load_entry(repo)
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
