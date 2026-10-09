#!/usr/bin/env python3
"""ghreview: helpers for staging PENDING GitHub PR reviews via gh.

Subcommands:
  map            -R OWNER/REPO -n PR              JSON: per-file addressable-line ranges + flags
  extract        -R OWNER/REPO -n PR [PATH ...]   unified patches for the given files (all if none)
  show           -R OWNER/REPO -n PR --commit SHA PATH [--lines A:B]
                                                  a file (numbered) or directory at SHA, read
                                                  from GitHub; never from a local checkout
  delta          -R OWNER/REPO -n PR --commit SHA --since OLD
                                                  which changed files need re-reading after a
                                                  complete review recorded at OLD
  pending        -R OWNER/REPO -n PR              current user's PENDING review {id, node_id}, if any
  clear-pending  -R OWNER/REPO -n PR              DELETE the current user's PENDING review, if any
  threads        -R OWNER/REPO -n PR [--all]      JSON: unresolved review threads rooted by the
                                                  current user (--all includes resolved ones)
  resolve-thread -R OWNER/REPO -n PR --thread-id PRRT_… [--dry-run]
                                                  mark a thread resolved (immediate, not staged;
                                                  needs PR authorship or write access)
  reply          -R OWNER/REPO -n PR --thread-id PRRT_… --body-file FILE [--dry-run]
                                                  STAGE a reply into the current user's pending
                                                  review (created as an empty shell if absent)
  stage          -R OWNER/REPO -n PR --commit SHA --input FILE [--replace-pending]
                 [--since OLD] [--dry-run]
                 validate comments against the diff, then create ONE pending review
                 (payload deliberately has NO "event" field -> review stays PENDING)
  verdict        -R OWNER/REPO -n PR --commit SHA
                 the recorded verdict for this PR and whether it still stands

-R is canonicalised once to GitHub's full_name, so receipts and verdicts do not
depend on how the repository was spelled.

stage --input file: {"verdict": "ready-to-merge"|"neutral"|"seriously-problematic",
                     "reviewed": [changed paths actually read],
                     "comments": [{"path", "line", "side", "body",
                                   "start_line"?, "start_side"?, "confidence"?}, ...],
                     "notes": ["review-level reservation", ...]?,
                     "verdict_override": "why a standing ready-to-merge no longer holds"?}
line = absolute line number in the new file for side RIGHT (old file for LEFT).
A multi-line range must lie inside one hunk; otherwise it is staged as a
single-line comment on `line` and listed under `narrowed`. A comment whose
`confidence` (0-100) is below MIN_CONFIDENCE is not staged; it is listed
under `filtered`, which is a decision, not an omission.
A line that is not addressable in the diff cannot be an inline comment — one
bad line would 422 the entire review — so that finding is CARRIED in the
review body instead, with its path, line and the reason it could not anchor.
The body is private until the review is submitted, which is why it is the
right carrier and a public PR comment is not. Only input with nothing
renderable in it (a non-object, or no path or no text) is `omitted`.
`complete` means every finding reached the author: staged inline or carried.
`coverage` lists changed files the input did not declare reviewed.

Verdict rules are enforced before anything is mutated: neutral and
seriously-problematic need at least one pending comment or note; ready-to-merge
needs every changed non-generated file reviewed; a recorded ready-to-merge
stands while the PR diff is unchanged and is only replaced with an explicit
verdict_override that is not template text. With --since, files whose patch is
unchanged since the complete review recorded at OLD count as covered (listed
under coverage.carried); every other changed file must be read again.

Generated files are lockfiles and known generated suffixes, plus whatever the
base branch's root .gitattributes marks linguist-generated (=false un-marks).
The base, not the head, is read so a PR cannot exempt its own files.

Read-only GETs are retried once on a transient failure (5xx, 429, timeout,
connection reset). Mutations are never retried.

Exit codes: 0 success; 1 API failure; 2 usage/input error;
3 refused — a pending review being cleared (clear-pending, or stage
--replace-pending) contains comments not staged by this script; pass --force
to discard them anyway.
"""
import argparse
import base64
import json
import re
import subprocess
import hashlib
import time
import os
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Only names that are generated by construction. Directory names such as
# build/, dist/ or vendor/ hold hand-written code often enough (build scripts,
# patched vendored libraries) that exempting them from coverage would let real
# changes go unread; a repository that does generate into them says so with
# linguist-generated in .gitattributes, which is honoured below.
GENERATED_PATTERNS = [
    r"(^|/)package-lock\.json$", r"(^|/)npm-shrinkwrap\.json$", r"(^|/)yarn\.lock$",
    r"(^|/)pnpm-lock\.yaml$", r"(^|/)bun\.lockb?$", r"(^|/)Cargo\.lock$",
    r"(^|/)Gemfile\.lock$", r"(^|/)poetry\.lock$", r"(^|/)Pipfile\.lock$",
    r"(^|/)uv\.lock$", r"(^|/)go\.sum$", r"(^|/)composer\.lock$", r"(^|/)flake\.lock$",
    r"(^|/)Podfile\.lock$", r"(^|/)pubspec\.lock$", r"(^|/)mix\.lock$",
    r"\.min\.(js|css)$", r"\.(js|mjs|cjs|css)\.map$",
    r"\.pb\.(go|cc|h)$", r"_pb2(_grpc)?\.pyi?$", r"\.generated\.",
]
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

GH_TIMEOUT = 30
# A paginated call is one gh process for every page; a 3000-file PR is 30
# pages at per_page=100, which does not fit the single-request timeout.
GH_PAGINATED_TIMEOUT = 180
RETRY_DELAY = 2.0
TRANSIENT_ERROR = re.compile(
    r"HTTP (5\d\d|429)|timed? ?out|connection (reset|refused)|unexpected EOF|TLS handshake"
    r"|temporarily unavailable|bad gateway|service unavailable",
    re.IGNORECASE)

# The lens confidence the procedure asks for; a staged comment that declares a
# lower one is filtered rather than posted.
MIN_CONFIDENCE = 80
SNAP_TOLERANCE = 3  # lines outside a hunk boundary still snapped into it
SNAP_MAX_DISTANCE = 10  # beyond this from the requested line, drop instead of snapping

MARKER = "<!-- leos-agent:review-pr -->"

# The reviewer's procedure caps a review at 15 comments; this is the script's
# own backstop well above it, so a reviewer talked past its cap by a hostile
# diff still cannot blanket a pull request.
MAX_STAGE_COMMENTS = 50

# A finding that cannot be anchored is still a finding. It goes in the review
# BODY, which is private until the review is submitted -- that privacy is the
# whole reason the body is the right carrier and a public PR comment is not.
# GitHub caps a review body at 65536 characters; leave room for a preamble.
MAX_BODY_CHARS = 60000


def _mark(body):
    """Tag a comment body as tool-created, so clear-pending can tell it apart
    from anything Leo hand-drafted into the same pending review."""
    body = (body or "").rstrip()
    return body if MARKER in body else f"{body}\n\n{MARKER}"


def idempotent(args):
    """A REST GET: gh api with no method override, no body and no fields."""
    if not args or args[0] != "api" or (len(args) > 1 and args[1] == "graphql"):
        return False
    return not any(a in ("--method", "-X", "--input", "-f", "-F", "--field", "--raw-field") for a in args)


def gh(args, payload=None, timeout=None, retry=None, binary=False):
    """Run gh, return stdout. Raises CalledProcessError with stderr attached.

    A read-only call (a REST GET, or a GraphQL query whose caller says so) is
    retried once after a transient failure. A mutation never is: its outcome is
    uncertain, and repeating it could stage a second review.
    """
    if timeout is None:
        timeout = GH_PAGINATED_TIMEOUT if "--paginate" in args else GH_TIMEOUT
    attempts = 2 if (idempotent(args) if retry is None else retry) else 1
    for attempt in range(attempts):
        last = attempt + 1 == attempts
        try:
            proc = subprocess.run(["gh"] + args, input=payload, capture_output=True,
                                  text=not binary, timeout=timeout)
        except subprocess.TimeoutExpired:
            if last:
                raise
            time.sleep(RETRY_DELAY)
            continue
        if proc.returncode == 0:
            return proc.stdout
        stderr = proc.stderr.decode("utf-8", "replace") if binary else proc.stderr
        if not last and TRANSIENT_ERROR.search(stderr or ""):
            time.sleep(RETRY_DELAY)
            continue
        raise subprocess.CalledProcessError(proc.returncode, proc.args, proc.stdout, stderr)


def ndjson(out):
    """Objects from gh --jq '.[]' output, one per line.

    Split on newline only: a JSON string may legally hold U+2028, U+2029 or
    U+0085 unescaped, and str.splitlines() would cut an object in two there.
    """
    return [json.loads(line) for line in out.split("\n") if line.strip()]


def fetch_files(repo, pr):
    """List PR files as dicts. --paginate + --jq '.[]' yields NDJSON."""
    out = gh(["api", f"repos/{repo}/pulls/{pr}/files?per_page=100", "--paginate", "--jq", ".[]"])
    return ndjson(out)


def is_generated(path, rules=None):
    """Generated by the repository's own attributes first, then by name."""
    marked = linguist_generated(path, rules or [])
    if marked is not None:
        return marked
    return any(re.search(p, path) for p in GENERATED_PATTERNS)


def _glob_regex(pattern):
    """A gitattributes pattern as (anchored, compiled regex), or None.

    Same rules as .gitignore without negation: a pattern with no slash matches
    a basename at any depth, one with a slash is anchored at the root, `**`
    spans directories, and a trailing-slash pattern names a directory, which
    gitattributes never applies to the files inside it.
    """
    if not pattern or pattern.endswith("/"):
        return None
    anchored = "/" in pattern
    pattern = pattern.lstrip("/")
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 2:]:
            end = pattern.index("]", i + 2)
            body = pattern[i + 1:end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
        elif pattern[i] == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return anchored, re.compile("".join(out) + r"\Z")


def attribute_rules(text):
    """[(anchored, regex, value)] for every line that sets linguist-generated."""
    rules = []
    for raw in (text or "").split("\n"):
        tokens = raw.strip().split()
        if not tokens or tokens[0].startswith("#") or tokens[0].startswith("[attr]"):
            continue
        pattern = tokens[0].strip('"')
        for token in tokens[1:]:
            name, value = token, True
            if token.startswith("-"):
                name, value = token[1:], False
            elif token.startswith("!"):
                name, value = token[1:], None
            elif "=" in token:
                name, setting = token.split("=", 1)
                value = setting.lower() not in ("false", "0", "no")
            if name != "linguist-generated":
                continue
            compiled = _glob_regex(pattern)
            if compiled:
                rules.append((compiled[0], compiled[1], value))
    return rules


def linguist_generated(path, rules):
    """True/False when an attribute line decides; None when none does. Last match wins."""
    decided = None
    base = path.rsplit("/", 1)[-1]
    for anchored, regex, value in rules:
        if regex.match(path if anchored else base):
            decided = value
    return decided


def patch_lines(patch):
    """A patch split the way git and GitHub count its lines: on newline only.

    str.splitlines() also breaks on form feed, vertical tab, U+001C-U+001E,
    U+0085, U+2028/9 and a lone carriage return, all of which git keeps inside
    a line; that shifted every later line number by one per occurrence. A
    trailing newline ends the last line rather than starting another.
    """
    lines = patch.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_patch(patch):
    """Walk unified-diff hunks -> addressable lines per side + hunk ranges.

    RIGHT (new file) is addressable on added and context lines; LEFT (old
    file) only on deleted lines — matching what the GitHub review UI accepts.
    """
    right, left = set(), set()
    hunks = []  # {"r": (start, end), "l": (start, end)}
    old_ln = new_ln = 0
    r_start = l_start = None

    def close_hunk():
        if r_start is not None:
            hunks.append({"r": (r_start, new_ln - 1), "l": (l_start, old_ln - 1)})

    for line in patch_lines(patch):
        m = HUNK_RE.match(line)
        if m:
            close_hunk()
            old_ln, new_ln = int(m.group(1)), int(m.group(3))
            r_start, l_start = new_ln, old_ln
        elif line.startswith("+"):
            right.add(new_ln)
            new_ln += 1
        elif line.startswith("-"):
            left.add(old_ln)
            old_ln += 1
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
        elif r_start is not None:
            right.add(new_ln)
            new_ln += 1
            old_ln += 1
    close_hunk()
    return {"right": right, "left": left, "hunks": hunks}


def build_maps(files):
    return {
        f["filename"]: parse_patch(f["patch"]) if f.get("patch") else None
        for f in files
    }


def ranges(nums):
    """Compress a set of ints into [start, end] ranges for compact output."""
    out = []
    for n in sorted(nums):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def snap_line(diffmap, side, line):
    """Only exact anchors are safe; proximity is not semantic equivalence."""
    return line if line in diffmap.get(side.lower(), set()) else None


def same_hunk(diffmap, side, start, end):
    """GitHub accepts a multi-line comment only when both ends lie in one hunk;
    a range across two hunks makes it reject the entire review with a 422."""
    key = "r" if side == "RIGHT" else "l"
    return any(h[key][0] <= start and end <= h[key][1] for h in diffmap.get("hunks", []))


def validate_comments(comments, maps, narrowed=None):
    staged, snapped, dropped = [], [], []
    narrowed = [] if narrowed is None else narrowed
    for c in comments:
        if not isinstance(c, dict):
            dropped.append({"value": c, "reason": "comment must be an object"})
            continue
        path = c.get("path")
        raw_body = c.get("body")
        body = raw_body.strip() if isinstance(raw_body, str) else ""
        side = c.get("side", "RIGHT")
        line = c.get("line")
        if (
            not isinstance(path, str)
            or not path
            or not body
            or not isinstance(line, int)
            or isinstance(line, bool)
        ):
            dropped.append({**c, "reason": "missing path/line/body"})
            continue
        if side not in ("RIGHT", "LEFT"):
            dropped.append({**c, "reason": f"invalid side {side!r}; expected RIGHT or LEFT"})
            continue
        diffmap = maps.get(path)
        if diffmap is None:
            dropped.append({**c, "reason": "file not in diff (or binary/no patch)"})
            continue
        new_line = snap_line(diffmap, side, line)
        if new_line is None:
            dropped.append({**c, "reason": f"line {line} ({side}) not addressable in any hunk"})
            continue
        entry = {"path": path, "line": new_line, "side": side, "body": _mark(body)}
        # Multi-line ranges: keep only if the start anchors cleanly before the
        # end on the same side and in the same hunk; otherwise degrade to a
        # single-line comment on `line` and say so.
        start = c.get("start_line")
        if isinstance(start, int) and not isinstance(start, bool):
            start_side = c.get("start_side", side)
            snapped_start = snap_line(maps[path], start_side, start)
            if (snapped_start is not None and snapped_start < new_line and start_side == side
                    and same_hunk(maps[path], side, snapped_start, new_line)):
                entry["start_line"] = snapped_start
                entry["start_side"] = start_side
            else:
                narrowed.append({"path": path, "start_line": start, "line": new_line, "side": side,
                                 "reason": "range is not inside one hunk on one side; staged on line only"})
        if new_line != line:
            snapped.append({"path": path, "from": line, "to": new_line})
        staged.append(entry)
    return staged, snapped, dropped


def representable(entry):
    """Can this dropped finding still be shown to the author?

    Yes when it kept a usable path and a usable body: the anchor failed, the
    finding did not. No for structurally malformed input -- a non-object, or a
    comment with no path or no text -- which has nothing to render.
    """
    if not isinstance(entry, dict):
        return False
    path, body = entry.get("path"), entry.get("body")
    return bool(isinstance(path, str) and path.strip() and isinstance(body, str) and body.strip())


def render_carried(findings):
    """(body, carried, overflow) for findings with no addressable line.

    Truncation is never silent: a finding that does not fit is returned as
    overflow and counted as omitted, because a finding nobody can read has not
    been preserved just because we tried.
    """
    if not findings:
        return "", [], []
    header = (MARKER + "\n\n## Findings without an addressable line\n\n"
              "Recorded here because this diff has no line to anchor them to. Not dropped.\n\n")
    carried, overflow, size = [], [], len(header)
    for entry in findings:
        text = str(entry.get("body", "")).strip()
        line = entry.get("line")
        where = "%s:%s" % (entry.get("path"), line if line is not None else "?")
        rendered = "- `%s` (%s) — %s\n%s\n\n" % (
            where, entry.get("side") or "RIGHT", entry.get("reason", "not addressable"),
            "\n".join("  > " + row for row in text.splitlines() or [""]),
        )
        if size + len(rendered) > MAX_BODY_CHARS:
            overflow.append(dict(entry, reason="did not fit in the review body; %s" % entry.get("reason", "not addressable")))
            continue
        carried.append(entry)
        size += len(rendered)
        header += rendered
    if overflow:
        header += "… %d further finding(s) did not fit; see the stage report.\n" % len(overflow)
    return header, carried, overflow


def current_login():
    return gh(["api", "user", "-q", ".login"]).strip()


def graphql(query, variables, retry=False):
    """Run a GraphQL query/mutation via gh. Int/bool variables go through -F (typed).

    retry=True only for a query; a mutation's outcome after a failure is unknown.
    """
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # bool before int: bool is a subclass of int, so this order matters.
        if isinstance(value, bool):
            args += ["-F", f"{key}={str(value).lower()}"]
        elif isinstance(value, int):
            args += ["-F", f"{key}={value}"]
        else:
            args += ["-f", f"{key}={value}"]
    return json.loads(gh(args, retry=retry))


REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# canonical full_name -> the spelling this invocation was given, so a receipt
# written under that spelling by an older release is still found.
GIVEN_SPELLING = {}


def canonical_repo(repo):
    """GitHub's own OWNER/REPO spelling for `repo`.

    Owner and repository names are case-insensitive and renamed repositories
    redirect, so the same PR can be named many ways. Receipts and verdicts are
    keyed by name; keyed by whatever was typed, `Foo/Bar` and `foo/bar` stopped
    recognising each other's drafts and decisions.
    """
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise ValueError("-R must be OWNER/REPO")
    # Lenses call `show` and `extract` many times in one review; one lookup an
    # hour per spelling is enough, and a rename only ever adds a redirect.
    cache = cache_path("repo", repo.lower())
    hit = read_cache(cache, FILES_CACHE_TTL)
    name = hit.get("full_name") if isinstance(hit, dict) else None
    if not (isinstance(name, str) and REPO_RE.fullmatch(name) and name.lower() == repo.lower()):
        name = gh(["api", f"repos/{repo}", "--jq", ".full_name"]).strip()
        if not REPO_RE.fullmatch(name):
            raise ValueError(f"GitHub returned no repository name for {repo}")
        write_cache(cache, {"full_name": name})
    if name != repo:
        GIVEN_SPELLING[name] = repo
    return name


def pending_review(repo, pr):
    """The current user's PENDING review as {"id", "node_id"}, or None.

    REST node_id is the GraphQL PullRequestReview id (verified identical) —
    usable directly in mutations.
    """
    out = gh(["api", f"repos/{repo}/pulls/{pr}/reviews?per_page=100", "--paginate", "--jq", ".[]"])
    login = current_login()
    for review in ndjson(out):
        if review.get("state") == "PENDING" and review.get("user", {}).get("login") == login:
            return review
    return None


def review_comments(repo, pr, review_id):
    """All comments on a (pending) review, oldest first."""
    out = gh(["api", f"repos/{repo}/pulls/{pr}/reviews/{review_id}/comments?per_page=100",
              "--paginate", "--jq", ".[]"])
    return ndjson(out)


# A receipt is only useful while its pending review is still around to be
# recognised. Nothing ever deleted them, so the directory grew for the life of
# the machine; a bounded sweep on write keeps it to recent reviews. A receipt
# is touched whenever it proves ownership, so a long-lived draft keeps it.
RECEIPT_TTL = 30 * 86400


def receipt_path(repo, pr, review_id):
    from state import _data_root
    key = hashlib.sha256(f"{repo}:{pr}:{review_id}".encode()).hexdigest()
    return Path(_data_root()) / "reviews" / (key + ".json")


def receipt_file(repo, pr, review_id):
    """The receipt path to read: the canonical one, else one written under the
    spelling this invocation was given (by a release that did not canonicalise)."""
    path = receipt_path(repo, pr, review_id)
    given = GIVEN_SPELLING.get(repo)
    if not path.exists() and given:
        legacy = receipt_path(given, pr, review_id)
        if legacy.exists():
            return legacy
    return path


def prune_receipts(keep=None, now=None):
    """Drop receipts and draft backups past RECEIPT_TTL. Never the current one."""
    now = time.time() if now is None else now
    keep = {str(keep)} if keep else set()
    try:
        entries = sorted(receipt_path("x", 0, 0).parent.iterdir())
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        if str(entry) in keep or not entry.name.endswith(".json"):
            continue
        try:
            if now - entry.stat().st_mtime > RECEIPT_TTL:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# Bumped whenever what a receipt hashes changes. A receipt from another scheme
# cannot prove ownership either way, so it is refused with its own reason.
RECEIPT_SCHEME = 2


def _normal_text(text):
    return (text or "").replace("\r\n", "\n").strip()


def comment_identity(comment):
    """The part of a draft comment that both sides of the ownership check agree on.

    For a PENDING review GitHub returns line, side, start_line, original_line
    and original_side as null; only position and original_position are set.
    The staged input has the reverse. Anchors therefore cannot be part of the
    fingerprint. Path, text and reply target can, and they are exactly what a
    hand edit changes: a rewritten body, an added comment, a deleted one. A
    pending comment cannot be moved, so nothing is lost by leaving anchors out.
    """
    return {"path": comment.get("path") or "", "body": _normal_text(comment.get("body")),
            "in_reply_to_id": comment.get("in_reply_to_id")}


def receipt_rows(comments):
    """Per-comment path plus content hash, sorted: what the receipt stores."""
    rows = [{"path": comment_identity(c)["path"],
             "sha256": hashlib.sha256(json.dumps(comment_identity(c), sort_keys=True).encode()).hexdigest()}
            for c in comments]
    return sorted(rows, key=lambda row: (row["path"], row["sha256"]))


def review_fingerprint(body, comments):
    rows = [row["sha256"] for row in receipt_rows(comments)]
    return hashlib.sha256(json.dumps([RECEIPT_SCHEME, _normal_text(body), rows]).encode()).hexdigest()


def restore_anchor(comment):
    """A backed-up draft comment as a create-review payload entry.

    A pending comment re-read from GitHub has no line, only a position, so a
    payload built from line alone would 422 and lose the restoration.
    """
    entry = {"path": comment.get("path"), "body": comment.get("body")}
    if comment.get("line") is not None:
        entry.update({key: comment[key] for key in ("line", "side", "start_line", "start_side")
                      if comment.get(key) is not None})
    elif comment.get("position") is not None:
        entry["position"] = comment["position"]
    return entry


def remember_review(repo, pr, review, comments, body=""):
    from state import atomic_write
    path = receipt_path(repo, pr, review["id"])
    atomic_write(str(path), {"scheme": RECEIPT_SCHEME, "review_id": review["id"],
                             "fingerprint": review_fingerprint(body, comments),
                             "comments": receipt_rows(comments)})
    prune_receipts(keep=path)


def remember_posted(repo, pr, review, fallback, body):
    """Receipt for a review just created, from what GitHub now says it holds.

    The re-read is the same view pending_snapshot will compare against later.
    If it fails, the posted input is an equivalent stand-in because the
    fingerprint ignores anchors, the only field the two views disagree on.
    """
    try:
        comments = review_comments(repo, pr, review["id"])
    except (OSError, ValueError, subprocess.SubprocessError):
        comments = fallback
    remember_review(repo, pr, review, comments, body)


def owns(review, comments, receipt):
    return (receipt.get("scheme") == RECEIPT_SCHEME
            and receipt.get("fingerprint") == review_fingerprint(review.get("body"), comments))


def read_receipt(repo, pr, review_id):
    try:
        receipt = json.loads(receipt_file(repo, pr, review_id).read_text())
    except (OSError, ValueError):
        return {}
    return receipt if isinstance(receipt, dict) else {}


def touch_receipt(repo, pr, review_id):
    """Keep a receipt that just proved ownership out of the age-based sweep."""
    try:
        os.utime(str(receipt_file(repo, pr, review_id)))
    except OSError:
        pass


def ownership_refusal(review, comments, receipt, owned, recoverable):
    refusal = {"refused": True, "review_id": review["id"], "total_count": len(comments)}
    if receipt and receipt.get("scheme") != RECEIPT_SCHEME:
        refusal.update(legacy_receipt=True, reason=(
            "pending review was staged before receipts matched GitHub's pending-comment shape, "
            "so ownership cannot be checked; ask the user to inspect the draft and approve a "
            "one-time --force; preserve it until they do"))
    elif not receipt:
        refusal["reason"] = "pending review has no ownership receipt (hand-drafted or staged elsewhere); preserve it"
    elif not owned:
        stored = {(row.get("path"), row.get("sha256")) for row in receipt.get("comments") or []
                  if isinstance(row, dict)}
        current = {(row["path"], row["sha256"]) for row in receipt_rows(comments)}
        refusal["changed_paths"] = sorted({path for path, _ in stored ^ current})
        refusal["reason"] = "pending review was edited after staging; preserve it"
    else:
        refusal["reason"] = ("pending review contains a reply whose thread could not be found, so "
                             "it cannot be restored if replacement fails; preserve it")
    return refusal


def pending_snapshot(repo, pr, force=False):
    review = pending_review(repo, pr)
    if review is None:
        return None, None
    comments = review_comments(repo, pr, review["id"])
    receipt = read_receipt(repo, pr, review["id"])
    owned = owns(review, comments, receipt)
    if owned:
        touch_receipt(repo, pr, review["id"])
    # A reply cannot come back as a root comment, so recovery re-adds it to its
    # thread; that needs the thread's node id, which only GraphQL knows.
    replies = [c for c in comments if c.get("in_reply_to_id")]
    targets = reply_targets(repo, pr, replies) if replies and (owned or force) else []
    recoverable = targets is not None
    if not force and (not owned or not recoverable):
        return None, ownership_refusal(review, comments, receipt, owned, recoverable)
    return {"review": review, "comments": comments, "replies": targets or [], "forced": not owned}, None


def reply_targets(repo, pr, replies):
    """[{thread_id, in_reply_to_id, body}] per reply in a draft, or None when one
    cannot be placed. A pending reply's REST in_reply_to_id is the REST id of
    the comment it answers, which GraphQL exposes as that comment's databaseId."""
    threads = {}
    for thread in fetch_threads(repo, pr):
        nodes = thread.get("comments", {}).get("nodes", [])
        # A thread rooted in a draft disappears with the draft, so a reply to
        # it has nothing to go back onto: leave it unplaceable.
        if not nodes or (nodes[0].get("pullRequestReview") or {}).get("state") == "PENDING":
            continue
        for comment in nodes:
            if comment.get("databaseId") is not None:
                threads[comment["databaseId"]] = thread["id"]
    targets = []
    for reply in replies:
        thread = threads.get(reply.get("in_reply_to_id"))
        if not thread:
            return None
        targets.append({"thread_id": thread, "in_reply_to_id": reply["in_reply_to_id"],
                        "body": reply.get("body") or ""})
    return targets


def clear_pending_guarded(repo, pr, force):
    snapshot, refusal = pending_snapshot(repo, pr, force)
    if refusal:
        return None, refusal
    if snapshot is None:
        return {"deleted": None}, None
    review = snapshot["review"]
    from state import atomic_write
    backup = receipt_path(repo, pr, review["id"]).with_suffix(".backup.json")
    atomic_write(str(backup), snapshot)
    gh(["api", f"repos/{repo}/pulls/{pr}/reviews/{review['id']}", "--method", "DELETE"])
    return {"deleted": review["id"], "forced": snapshot["forced"], "backup": str(backup)}, None


def current_head(repo, pr):
    return gh(["api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha"]).strip()


def require_head(repo, pr, commit):
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("a full 40-character reviewed head SHA is required")
    actual = current_head(repo, pr)
    if actual != commit:
        raise ValueError(f"PR head changed from {commit} to {actual}; re-review before staging or resolving")


def base_sha(repo, pr):
    return gh(["api", f"repos/{repo}/pulls/{pr}", "--jq", ".base.sha"]).strip()


# The file list at one head, kept briefly so the lenses' `extract` calls and
# `map` do not each re-page a large PR. Only those read-only calls use it;
# `stage` and `verdict` always fetch, so no decision ever rests on a cached
# list. The key includes the head; the TTL bounds staleness from a moved base.
FILES_CACHE_TTL = 3600
CONTENT_CACHE_TTL = 7 * 86400  # content addressed by commit SHA never changes


def cache_dir():
    from state import _data_root
    return Path(_data_root()) / "reviews" / "cache"


def cache_path(kind, *parts):
    key = hashlib.sha256(json.dumps([kind] + [str(p) for p in parts]).encode()).hexdigest()
    return cache_dir() / f"{kind}-{key}.json"


def read_cache(path, ttl):
    try:
        if time.time() - path.stat().st_mtime > ttl:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_cache(path, data):
    """Best effort: a cache that cannot be written only costs a refetch."""
    from state import atomic_write
    try:
        os.makedirs(str(path.parent), mode=0o700, exist_ok=True)
        atomic_write(str(path), data)
        prune_cache()
    except OSError:
        pass


def prune_cache(now=None):
    now = time.time() if now is None else now
    try:
        entries = list(cache_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        ttl = FILES_CACHE_TTL if entry.name.startswith("files-") else CONTENT_CACHE_TTL
        try:
            if entry.name.endswith(".json") and now - entry.stat().st_mtime > ttl:
                entry.unlink()
        except OSError:
            continue


def listed_for_other_head(files, commit):
    """Paths whose listing entry names another commit than `commit`.

    Right after a push, pulls/N can already report the new head while the
    files listing still describes the old one. Each entry's contents_url
    carries `?ref=<head>` (a removed file's names the base side, so it is
    skipped); an entry naming another commit is a stale listing, not this diff.
    """
    stale = []
    for f in files:
        url = f.get("contents_url")
        if f.get("status") == "removed" or not isinstance(url, str):
            continue
        ref = parse_qs(urlsplit(url).query).get("ref", [None])[0]
        if ref != commit:
            stale.append(f.get("filename"))
    return stale


def pinned_files(repo, pr, commit, cached=False):
    require_head(repo, pr, commit)
    path = cache_path("files", repo, pr, commit)
    if cached:
        files = read_cache(path, FILES_CACHE_TTL)
        if isinstance(files, list):
            return files
    files = fetch_files(repo, pr)
    if listed_for_other_head(files, commit):
        time.sleep(RETRY_DELAY)
        files = fetch_files(repo, pr)
        stale = listed_for_other_head(files, commit)
        if stale:
            raise ValueError("GitHub's file listing still describes another head than %s (%s); "
                             "retry once it catches up" % (commit, ", ".join(stale[:5])))
    require_head(repo, pr, commit)
    write_cache(path, files)
    return files


def not_found(error):
    return isinstance(error, subprocess.CalledProcessError) and "HTTP 404" in (error.stderr or "")


def contents(repo, commit, path):
    """GET contents/PATH at a commit: a dict for a file, symlink or submodule, a
    list for a directory. Raises ValueError when PATH does not exist there."""
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("a full 40-character commit SHA is required")
    path = (path or "").strip("/")
    if any(part in ("..", ".") for part in path.split("/")) or "\x00" in path:
        raise ValueError("path must be relative to the repository root")
    try:
        out = gh(["api", f"repos/{repo}/contents/{quote(path)}?ref={commit}"])
    except subprocess.CalledProcessError as error:
        if not_found(error):
            raise ValueError(f"{path or '/'} does not exist at {commit}") from None
        raise
    return json.loads(out)


def file_bytes(repo, commit, path, meta):
    """A file's bytes: inline base64 up to 1 MB, the raw media type above that."""
    if meta.get("encoding") == "base64" and meta.get("content"):
        return base64.b64decode(meta["content"])
    return gh(["api", "-H", "Accept: application/vnd.github.raw+json",
               f"repos/{repo}/contents/{quote(path.strip('/'))}?ref={commit}"],
              binary=True, timeout=GH_PAGINATED_TIMEOUT)


def text_at(repo, commit, path, max_bytes):
    """A text file at commit, or None when it is absent. Cached: SHA-addressed."""
    cache = cache_path("text", repo, commit, path)
    hit = read_cache(cache, CONTENT_CACHE_TTL)
    if isinstance(hit, dict) and "text" in hit:
        return hit["text"]
    try:
        meta = contents(repo, commit, path)
    except ValueError:
        text = None
    else:
        if not isinstance(meta, dict) or meta.get("type") != "file" or (meta.get("size") or 0) > max_bytes:
            text = None
        else:
            text = file_bytes(repo, commit, path, meta).decode("utf-8", "replace")
    write_cache(cache, {"text": text})
    return text


GITATTRIBUTES_MAX_BYTES = 256_000


def generated_rules(repo, pr):
    """linguist-generated rules from the base branch's root .gitattributes."""
    base = base_sha(repo, pr)
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        return []
    return attribute_rules(text_at(repo, base, ".gitattributes", GITATTRIBUTES_MAX_BYTES))


THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line originalLine
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id databaseId author { login } body createdAt
              pullRequestReview { id state }
            }
          }
        }
      }
    }
  }
}"""

THREAD_COMMENTS_QUERY = """
query($thread: ID!, $cursor: String) {
  node(id: $thread) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId author { login } body createdAt pullRequestReview { id state } }
      }
    }
  }
}"""

RESOLVE_MUTATION = """
mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) { thread { id isResolved } }
}"""

REPLY_MUTATION = """
mutation($thread: ID!, $review: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
    input: {pullRequestReviewThreadId: $thread, pullRequestReviewId: $review, body: $body}
  ) { comment { id } }
}"""


def fetch_threads(repo, pr):
    owner, name = repo.split("/", 1)
    nodes, cursor, seen_pages = [], None, set()
    while True:
        variables = {"owner": owner, "name": name, "number": int(pr)}
        if cursor:
            variables["cursor"] = cursor
        conn = graphql(THREADS_QUERY, variables, retry=True)["data"]["repository"]["pullRequest"]["reviewThreads"]
        for thread in conn["nodes"]:
            comments = thread["comments"]
            seen = set()
            while comments.get("pageInfo", {}).get("hasNextPage"):
                after = comments["pageInfo"]["endCursor"]
                if not after or after in seen:
                    raise ValueError("thread comment pagination did not advance")
                seen.add(after)
                page = graphql(THREAD_COMMENTS_QUERY, {"thread": thread["id"], "cursor": after}, retry=True)["data"]["node"]["comments"]
                comments["nodes"].extend(page["nodes"])
                comments["pageInfo"] = page["pageInfo"]
            nodes.append(thread)
        if not conn["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = conn["pageInfo"]["endCursor"]
        if not cursor or cursor in seen_pages:
            raise ValueError("review thread pagination did not advance")
        seen_pages.add(cursor)


def post_review(repo, pr, commit, staged, body=""):
    # No "event" field -> the review stays PENDING. `comments` is omitted rather
    # than sent empty when nothing anchored: that is the payload shape cmd_reply
    # already relies on to open an empty pending shell.
    fields = {"commit_id": commit, "body": body}
    if staged:
        fields["comments"] = staged
    payload = json.dumps(fields)
    out = gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "--method", "POST", "--input", "-"], payload)
    return json.loads(out)


READY = "ready-to-merge"
NEUTRAL = "neutral"
BLOCKING = "seriously-problematic"
VERDICTS = (READY, NEUTRAL, BLOCKING)
# Shared with watch_review.py, so `watch_review.py state` shows every verdict,
# whether it came from the watcher or from a manual review-pr pass.
VERDICT_STATE = "review-watcher"
MAX_NOTES_CHARS = 4000


def diff_fingerprint(files):
    """Identity of the PR's change, not of its head commit.

    A merge from the base that leaves the change alone moves the head and can
    shift every hunk's line numbers, so hunk headers are dropped. Context and
    changed lines stay: if they differ, the PR is a different change. So a
    base change inside, or within GitHub's three context lines of, a PR hunk
    does change it, and so does any change to a file with no patch (binary, or
    too large), which falls back to its blob SHA.
    """
    rows = [_patch_row(f) for f in sorted(files, key=lambda f: f.get("filename") or "")]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _patch_row(f):
    patch = f.get("patch")
    if patch is None:
        content = "blob:%s" % f.get("sha")
    else:
        content = "\n".join("@@" if HUNK_RE.match(line) else line for line in patch_lines(patch))
    return [f.get("filename"), f.get("previous_filename"), f.get("status"), content]


def patch_identity(f):
    """One file's share of diff_fingerprint: equal exactly when the PR's change
    to that file is unchanged, hunk line numbers aside. Truncated: it is
    compared against a stored value, never used as a security token."""
    return hashlib.sha256(json.dumps(_patch_row(f)).encode()).hexdigest()[:24]


def carried_paths(files, prior, since):
    """Paths whose patch is unchanged since the complete review at `since`.

    Raises when no such review is recorded, so --since never quietly widens
    coverage: the reviewer then reads every changed file as usual.
    """
    if not isinstance(since, str) or not re.fullmatch(r"[0-9a-f]{40}", since):
        raise ValueError("--since needs the full 40-character SHA of the earlier reviewed head")
    patches = (prior or {}).get("patches")
    if not isinstance(patches, dict) or since not in ((prior or {}).get("head"), (prior or {}).get("carried_to")):
        raise ValueError("no review with per-file coverage is recorded at %s; read every changed file "
                         "and stage without --since" % since)
    return {f["filename"] for f in files if patches.get(f["filename"]) == patch_identity(f)}


def review_coverage(files, reviewed, rules=None, carried=None, since=None):
    """Which changed files the reviewer declared read, and which it did not.

    `carried` files were read at an earlier head whose patch for them is
    identical; they count as covered and are listed, so the record shows which
    files this pass actually read and which it relied on.
    """
    declared = {path for path in reviewed if isinstance(path, str)}
    carried = set(carried or ())
    changed = [f["filename"] for f in files]
    generated = {path for path in changed if is_generated(path, rules)}
    unreviewed = [path for path in changed if path not in declared and path not in generated
                  and path not in carried]
    coverage = {"files_changed": len(changed),
                "files_reviewed": sum(path in declared for path in changed),
                "files_skipped_generated": [path for path in changed if path not in declared and path in generated],
                "unreviewed": unreviewed,
                "complete": not unreviewed}
    if since:
        coverage["carried_from"] = since
        coverage["carried"] = [path for path in changed if path not in declared and path in carried
                               and path not in generated]
    return coverage


def covered_patches(files, coverage, reviewed):
    """{path: patch identity} for every file this pass covered, for the next --since."""
    declared = {path for path in reviewed if isinstance(path, str)}
    covered = declared | set(coverage.get("carried") or ())
    return {f["filename"]: patch_identity(f) for f in files if f["filename"] in covered}


def load_verdict(repo, pr):
    """The newest verdict for this PR under any spelling of the repository.

    Older releases keyed verdicts by whatever -R was given; the newest record
    under a case-insensitively equal key is the decision that stands.
    """
    from state import load, state_file
    best = None
    for key, entry in load(state_file(VERDICT_STATE)).items():
        if not isinstance(entry, dict) or key.lower() != repo.lower():
            continue
        prior = (entry.get("verdicts") or {}).get(str(pr))
        if not isinstance(prior, dict):
            continue
        at = prior.get("at")
        rank = (at if isinstance(at, (int, float)) and not isinstance(at, bool) else 0, key == repo)
        if best is None or rank > best[0]:
            best = (rank, prior)
    return best[1] if best else None


def save_verdict(repo, pr, record):
    from state import _locked, atomic_write, load, state_file
    path = state_file(VERDICT_STATE)
    with _locked(path):
        data = load(path)
        data.setdefault(repo, {}).setdefault("verdicts", {})[str(pr)] = record
        atomic_write(path, data)


def standing_ready(prior, diff):
    """A recorded ready-to-merge holds until the PR diff itself changes."""
    return bool(prior) and prior.get("verdict") == READY and bool(diff) and prior.get("diff") == diff


def stage_request(data):
    """(comments, notes, reviewed, verdict, override) from a stage input file."""
    if isinstance(data, list):
        data = {"comments": data}
    if not isinstance(data, dict):
        raise ValueError("stage input must be an object with a comments list")
    comments = data.get("comments") or []
    if not isinstance(comments, list):
        raise ValueError("comments must be a list")
    notes = data.get("notes") or []
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise ValueError("notes must be a list of strings")
    notes = [n.strip() for n in notes if n.strip()]
    if sum(len(n) for n in notes) > MAX_NOTES_CHARS:
        raise ValueError("notes exceed %d characters; anchor findings as comments instead" % MAX_NOTES_CHARS)
    reviewed = data.get("reviewed") or []
    if not isinstance(reviewed, list):
        raise ValueError("reviewed must be a list of changed paths")
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError("stage input needs a verdict: one of %s" % ", ".join(VERDICTS))
    override = data.get("verdict_override")
    if override is not None and (not isinstance(override, str) or not override.strip()):
        raise ValueError("verdict_override must state why the prior decision no longer holds")
    if override is not None and templated(override):
        raise ValueError("verdict_override looks like template text; name the newly verified defect "
                         "(path:line and what fails), or leave the field out")
    return comments, notes, reviewed, verdict, override


# Text an override copied from an example, or a placeholder, would contain.
# Narrow on purpose: it cannot judge an override, only refuse one nobody wrote.
TEMPLATE_OVERRIDE = re.compile(
    r"only when overriding|^\W*(todo|tbd|n/?a|none|override|reason|defect|x+)\W*$"
    r"|^[\s.…_-]*$|^\s*<[^<>]*>\s*$", re.IGNORECASE)


def templated(text):
    return bool(TEMPLATE_OVERRIDE.search(text.strip())) or len("".join(text.split())) < 8


def confidence_filter(comments):
    """(kept, filtered): a comment declaring confidence below MIN_CONFIDENCE is
    held back. One without the field is the reviewer's own verified finding."""
    kept, filtered = [], []
    for c in comments:
        value = c.get("confidence") if isinstance(c, dict) else None
        if value is None:
            kept.append(c)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ValueError("comment confidence must be a number from 0 to 100")
        if value < MIN_CONFIDENCE:
            filtered.append({"path": c.get("path"), "line": c.get("line"), "confidence": value,
                             "reason": "confidence below %d; not staged" % MIN_CONFIDENCE})
        else:
            kept.append({k: v for k, v in c.items() if k != "confidence"})
    return kept, filtered


def verdict_refusal(verdict, pending, coverage, prior, diff, override):
    """Why this verdict may not be staged, or None. Checked before any mutation."""
    if verdict == NEUTRAL and pending == 0:
        return ("neutral needs at least one pending comment or note: stage the reservation "
                "so the author sees it, or the verdict is ready-to-merge")
    if verdict == BLOCKING and pending == 0:
        return ("seriously-problematic needs at least one pending comment or note stating the "
                "blocking issue, so the author sees what blocks the merge")
    if verdict == READY and coverage["unreviewed"]:
        return ("ready-to-merge needs every changed non-generated file reviewed; unreviewed: %s"
                % ", ".join(coverage["unreviewed"]))
    if verdict != READY and standing_ready(prior, diff) and not override:
        return ("a ready-to-merge verdict recorded at %s stands for this unchanged PR diff; keep it, "
                "or set verdict_override to say which newly found defect overrides it"
                % (prior.get("head") or "an earlier head"))
    return None


def render_notes(notes):
    if not notes:
        return ""
    return "## Review notes\n\n" + "".join("- %s\n" % n.replace("\n", "\n  ") for n in notes) + "\n"


def cmd_map(a):
    files = pinned_files(a.repo, a.pr, a.commit, cached=True)
    rules = generated_rules(a.repo, a.pr)
    report = []
    for f in files:
        diffmap = parse_patch(f["patch"]) if f.get("patch") else None
        report.append({
            "path": f["filename"],
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "generated": is_generated(f["filename"], rules),
            "has_patch": diffmap is not None,
            "right_ranges": ranges(diffmap["right"]) if diffmap else [],
            "left_ranges": ranges(diffmap["left"]) if diffmap else [],
        })
    reviewable = [f for f in report if f["has_patch"] and not f["generated"]]
    print(json.dumps({
        "commit": a.commit,
        "files": report,
        "reviewable_files": len(reviewable),
        "reviewable_lines": sum(f["additions"] + f["deletions"] for f in reviewable),
        # Still part of coverage: GitHub sent no patch (binary or too large),
        # so read them with `show` rather than skipping them.
        "read_with_show": [f["path"] for f in report if not f["has_patch"] and not f["generated"]
                           and f["status"] != "removed"],
    }, indent=1))


def cmd_extract(a):
    wanted = set(a.paths)
    seen = set()
    for f in pinned_files(a.repo, a.pr, a.commit, cached=True):
        if wanted and f["filename"] not in wanted:
            continue
        seen.add(f["filename"])
        header = f"--- {f['filename']} ({f['status']}, +{f['additions']} -{f['deletions']}"
        if f.get("patch"):
            print(header + ")")
            print(f["patch"])
        else:
            print(header + "; GitHub sent no patch: read the file with `show`)")
        print()
    for path in sorted(wanted - seen):
        print(f"--- {path} (not changed in this PR)\n")


SHOW_MAX_BYTES = 200_000          # printed whole; larger files need --lines
SHOW_RANGE_MAX_BYTES = 10_000_000  # fetched at most, to print a --lines range
# Rendered visibly: invisible and control characters are exactly what a
# reviewer must see (bidirectional overrides, zero-width joiners, a stray form
# feed), and raw they could also rewrite the terminal of whoever runs this.
INVISIBLE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f­​-‏‪-‮"
                       "  ⁠-⁤⁦-⁩﻿]")


def visible(text):
    return INVISIBLE.sub(lambda m: ("\\u%04x" if ord(m.group()) > 0xff else "\\x%02x") % ord(m.group()), text)


def line_range(spec, total):
    match = re.fullmatch(r"(\d+)?:(\d+)?", spec or "")
    if not match:
        raise ValueError("--lines takes START:END, either side optional, e.g. 40:90")
    start = int(match.group(1) or 1)
    end = min(int(match.group(2) or total), total)
    if start < 1 or start > max(total, 1) or end < start:
        raise ValueError(f"--lines {spec} is outside the file's {total} line(s)")
    return start, end


def render_file(path, commit, data, spec=None):
    """Numbered lines as GitHub counts them, so anchors can be checked directly."""
    if b"\x00" in data[:8192]:
        return f"--- {path} @ {commit[:12]}: binary, {len(data)} bytes; not shown\n"
    lines = patch_lines(data.decode("utf-8", "replace"))
    crlf = bool(lines) and all(line.endswith("\r") for line in lines)
    if crlf:
        lines = [line[:-1] for line in lines]
    start, end = line_range(spec, len(lines)) if spec else (1, len(lines))
    out = ["--- %s @ %s (%d line%s%s%s)" % (path, commit[:12], len(lines), "" if len(lines) == 1 else "s",
                                         ", CRLF" if crlf else "",
                                         "" if not spec else ", showing %d-%d" % (start, end))]
    out.extend("%6d\t%s" % (n, visible(lines[n - 1])) for n in range(start, end + 1))
    return "\n".join(out) + "\n"


def cmd_show(a):
    """A file or directory at an exact commit, from GitHub, never from a checkout.

    Reading code at the pinned SHA is the only way a finding is judged against
    the revision under review; a local working tree is usually at another one.
    """
    meta = contents(a.repo, a.commit, a.path)
    if isinstance(meta, list):
        rows = sorted(meta, key=lambda e: (e.get("type") != "dir", e.get("name") or ""))
        print("--- %s/ @ %s (%d entries)" % (a.path.strip("/") or ".", a.commit[:12], len(rows)))
        for entry in rows:
            suffix = "/" if entry.get("type") == "dir" else ""
            print("%s%s\t%s" % (visible(entry.get("name") or ""), suffix,
                                entry.get("size") if entry.get("type") == "file" else entry.get("type")))
        return
    kind = meta.get("type")
    if kind == "symlink":
        print(f"--- {a.path} @ {a.commit[:12]}: symlink to {visible(str(meta.get('target')))}")
        return
    if kind == "submodule":
        print(f"--- {a.path} @ {a.commit[:12]}: submodule at {meta.get('sha')} ({meta.get('submodule_git_url')})")
        return
    if kind != "file":
        raise ValueError(f"{a.path} is a {kind!r}, not a file or directory")
    size = meta.get("size") or 0
    limit = SHOW_RANGE_MAX_BYTES if a.lines else SHOW_MAX_BYTES
    if size > limit:
        raise ValueError(f"{a.path} is {size} bytes at {a.commit[:12]}; "
                         + ("pass --lines START:END to read part of it" if not a.lines
                            else f"too large to read here (limit {limit})"))
    sys.stdout.write(render_file(a.path, a.commit, file_bytes(a.repo, a.commit, a.path, meta), a.lines))


def cmd_delta(a):
    """What an incremental re-review must read again, and what it may carry."""
    files = pinned_files(a.repo, a.pr, a.commit)
    rules = generated_rules(a.repo, a.pr)
    prior = load_verdict(a.repo, a.pr)
    report = {"commit": a.commit, "since": a.since, "incremental": False}
    try:
        carried = carried_paths(files, prior, a.since)
    except ValueError as error:
        report["reason"] = str(error)
        print(json.dumps(report, indent=1))
        return
    blocker = incremental_blocker(a.repo, a.pr)
    if blocker:
        report["reason"] = blocker
        print(json.dumps(report, indent=1))
        return
    changed = [f["filename"] for f in files]
    report.update(incremental=True,
                  carry=[p for p in changed if p in carried and not is_generated(p, rules)],
                  review=[p for p in changed if p not in carried and not is_generated(p, rules)],
                  generated=[p for p in changed if is_generated(p, rules)])
    print(json.dumps(report, indent=1))


def incremental_blocker(repo, pr):
    """Why an incremental pass may not replace the user's pending draft, or None.

    Findings from the earlier pass on files that are now carried exist only in
    that draft until the user submits it. Replacing it with a review that does
    not re-read those files would discard them, so carry only once it is gone.
    """
    review = pending_review(repo, pr)
    if review is None:
        return None
    if review_comments(repo, pr, review["id"]) or (review.get("body") or "").strip():
        return ("a pending draft from an earlier pass still holds findings; an incremental "
                "review would replace it and lose those on unchanged files, so read every "
                "changed file (or submit or discard the draft first)")
    return None


def cmd_pending(a):
    review = pending_review(a.repo, a.pr)
    if review:
        print(json.dumps(review))


def cmd_clear_pending(a):
    result, refusal = clear_pending_guarded(a.repo, a.pr, a.force)
    if refusal:
        print(json.dumps(refusal, indent=1))
        sys.exit(3)
    print(json.dumps(result))


def cmd_threads(a):
    """Unresolved threads whose root comment is the current user's.

    Threads rooted in a PENDING review are excluded — those are staged
    drafts, not posted conversation. line is null for file-level threads.
    """
    login = current_login()
    threads = []
    for node in fetch_threads(a.repo, a.pr):
        comments = node["comments"]["nodes"]
        if not comments:
            continue
        root = comments[0]
        if (root.get("author") or {}).get("login") != login:
            continue
        if (root.get("pullRequestReview") or {}).get("state") == "PENDING":
            continue
        if node["isResolved"] and not a.all:
            continue
        last = comments[-1]
        threads.append({
            "thread_id": node["id"],
            "path": node["path"],
            "line": node["line"],
            "original_line": node.get("originalLine"),
            "is_resolved": node["isResolved"],
            "is_outdated": node["isOutdated"],
            "replies_after_mine": (last.get("author") or {}).get("login") != login,
            "comments": [{
                "author": (c.get("author") or {}).get("login"),
                "body": c["body"],
                "created_at": c["createdAt"],
            } for c in comments],
        })
    print(json.dumps({"my_login": login, "threads": threads}, indent=1))


def require_thread(repo, pr, thread_id, own=False):
    thread = next((t for t in fetch_threads(repo, pr) if t["id"] == thread_id), None)
    if thread is None:
        raise ValueError("thread does not belong to the specified repository and PR")
    if own:
        comments = thread.get("comments", {}).get("nodes", [])
        if not comments or (comments[0].get("author") or {}).get("login") != current_login():
            raise ValueError("automatic resolution is limited to threads started by the authenticated user")
        if (comments[0].get("pullRequestReview") or {}).get("state") == "PENDING":
            raise ValueError("cannot resolve a pending draft thread")
    return thread


def cmd_resolve_thread(a):
    require_head(a.repo, a.pr, a.commit)
    thread = require_thread(a.repo, a.pr, a.thread_id, own=True)
    evidence = json.loads(Path(a.evidence_file).read_text())
    if (evidence.get("thread_id") != a.thread_id or evidence.get("head_sha") != a.commit
            or not isinstance(evidence.get("explanation"), str) or not evidence["explanation"].strip()):
        raise ValueError("resolution evidence must identify this thread, reviewed head, and why the issue is addressed")
    if a.dry_run or thread.get("isResolved"):
        print(json.dumps({"would_resolve": a.thread_id, "commit": a.commit, "already_resolved": thread.get("isResolved", False)}))
        return
    require_head(a.repo, a.pr, a.commit)
    result = graphql(RESOLVE_MUTATION, {"thread": a.thread_id})
    print(json.dumps({"resolved": result["data"]["resolveReviewThread"]["thread"], "commit": a.commit}))


def cmd_reply(a):
    require_thread(a.repo, a.pr, a.thread_id)
    require_head(a.repo, a.pr, a.commit)
    with open(a.body_file) as fh:
        body = fh.read().strip()
    if not body:
        print("input error: empty reply body", file=sys.stderr)
        sys.exit(2)
    body = _mark(body)
    review = pending_review(a.repo, a.pr)
    if review and review.get("commit_id") != a.commit:
        raise ValueError("pending review is anchored to another head; re-review before adding replies")
    if a.dry_run:
        print(json.dumps({"would_reply_to": a.thread_id, "pending_review": review, "body": body}))
        return
    if review is None:
        # Empty pending shell: POST with no event and no comments stays PENDING.
        created = json.loads(gh(
            ["api", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--method", "POST", "--input", "-"],
            json.dumps({"commit_id": a.commit}),
        ))
        review = {"id": created["id"], "node_id": created["node_id"], "body": created.get("body") or ""}
        owned = True
    else:
        owned = owns(review, review_comments(a.repo, a.pr, review["id"]),
                     read_receipt(a.repo, a.pr, review["id"]))
    require_head(a.repo, a.pr, a.commit)
    result = graphql(REPLY_MUTATION, {
        "thread": a.thread_id, "review": review["node_id"], "body": body,
    })
    if owned:
        # Keep the receipt in step with our own reply, so a later refusal names
        # the reply rather than misreporting the draft as hand-edited.
        try:
            remember_review(a.repo, a.pr, review, review_comments(a.repo, a.pr, review["id"]),
                            review.get("body") or "")
        except (OSError, ValueError, subprocess.SubprocessError):
            print("reply staged; ownership receipt not refreshed", file=sys.stderr)
    print(json.dumps({
        "staged_reply": result["data"]["addPullRequestReviewThreadReply"]["comment"]["id"],
        "thread": a.thread_id,
        "pending_review": review["id"],
    }))


def cmd_verdict(a):
    """The recorded verdict for this PR, and whether it stands at the pinned head."""
    files = pinned_files(a.repo, a.pr, a.commit)
    diff = diff_fingerprint(files)
    prior = load_verdict(a.repo, a.pr)
    shown = {k: v for k, v in prior.items() if k != "patches"} if prior else None
    patches = (prior or {}).get("patches")
    earlier = (prior or {}).get("carried_to") or (prior or {}).get("head")
    print(json.dumps({"repo": a.repo, "pr": a.pr, "commit": a.commit, "diff_fingerprint": diff,
                      "prior": shown, "diff_unchanged": bool(prior) and prior.get("diff") == diff,
                      "standing_ready": standing_ready(prior, diff),
                      # An earlier head with per-file coverage: `delta --since` it.
                      "incremental_since": earlier if isinstance(patches, dict) and earlier != a.commit else None},
                     indent=1))


def restore_draft(repo, pr, cleared):
    """Put back an owned draft deleted for a replacement that then failed."""
    if not cleared or not cleared.get("deleted"):
        return
    backup = cleared["backup"]
    print(f"replacement failed; prior draft is recoverable from {backup}", file=sys.stderr)
    try:
        previous = json.loads(Path(backup).read_text())
        if pending_review(repo, pr) is None:
            old = previous["review"]
            roots = [c for c in previous["comments"] if not c.get("in_reply_to_id")]
            restored = post_review(repo, pr, old["commit_id"],
                                   [restore_anchor(c) for c in roots], old.get("body") or "")
            # Replies go back onto their threads, inside the restored draft. A
            # reply answers a submitted comment, so its in_reply_to_id (part of
            # the receipt) is the same before and after.
            lost = 0
            for reply in previous.get("replies") or []:
                try:
                    graphql(REPLY_MUTATION, {"thread": reply["thread_id"], "review": restored["node_id"],
                                             "body": reply["body"]})
                except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                    lost += 1
            if lost:
                print(f"{lost} reply(ies) could not be restored; recovery snapshot retained", file=sys.stderr)
            remember_posted(repo, pr, restored, previous["comments"],
                            restored.get("body") or old.get("body") or "")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("automatic restoration unavailable; recovery snapshot retained", file=sys.stderr)


def cmd_stage(a):
    with open(a.input) as fh:
        data = json.load(fh)
    comments, notes, reviewed, verdict, override = stage_request(data)
    if len(comments) > MAX_STAGE_COMMENTS:
        print(
            f"input error: {len(comments)} comments exceeds the cap of {MAX_STAGE_COMMENTS}; "
            "a review this wide should be narrowed, not staged",
            file=sys.stderr,
        )
        sys.exit(2)
    comments, filtered = confidence_filter(comments)
    since = getattr(a, "since", None)

    files = pinned_files(a.repo, a.pr, a.commit)
    rules = generated_rules(a.repo, a.pr)
    diff = diff_fingerprint(files)
    prior = load_verdict(a.repo, a.pr)
    carried_files = set()
    if since:
        carried_files = carried_paths(files, prior, since)
        blocker = incremental_blocker(a.repo, a.pr)
        if blocker:
            raise ValueError(blocker)
    coverage = review_coverage(files, reviewed, rules, carried_files, since)
    narrowed = []
    staged, snapped, dropped = validate_comments(comments, build_maps(files), narrowed)
    # A finding that failed to anchor is carried in the review body; only input
    # with nothing renderable in it is omitted. `complete` therefore means every
    # finding reached the author, not that every finding reached a diff line.
    body, carried, overflow = render_carried([d for d in dropped if representable(d)])
    if notes:
        body = (body or MARKER + "\n\n") + render_notes(notes)
    omitted = [d for d in dropped if not representable(d)] + overflow
    refusal = verdict_refusal(verdict, len(staged) + len(carried) + len(notes), coverage, prior, diff, override)
    if refusal:
        raise ValueError(refusal)
    report = {"repo": a.repo, "pr": a.pr, "commit": a.commit, "complete": False, "review_created": False,
              "staged": len(staged), "carried": len(carried), "notes": len(notes), "omitted": omitted,
              "snapped": snapped, "dropped": dropped, "narrowed": narrowed, "filtered": filtered,
              "verdict": verdict, "diff_fingerprint": diff, "coverage": coverage,
              "patches": covered_patches(files, coverage, reviewed)}
    if standing_ready(prior, diff) and verdict != READY:
        report.update(verdict_override=override.strip(), prior_verdict=prior)
    for d in carried:
        print(f"carried in review body: {d.get('path')}:{d.get('line')} — {d['reason']}", file=sys.stderr)
    for d in omitted:
        print(f"omitted: {d.get('path')}:{d.get('line')} — {d['reason']}", file=sys.stderr)

    if a.dry_run:
        report["comments"] = staged
        report["body"] = body
        print(json.dumps(report, indent=1))
        return
    if not staged and not carried and not notes:
        if comments:
            print(json.dumps({**report, "note": "nothing renderable in the input; no review created"}))
            return
        # Check the head before deleting anything: a moved head means this
        # review is stale, and the draft it would replace must survive.
        require_head(a.repo, a.pr, a.commit)
        cleared = None
        if a.replace_pending:
            cleared, refusal = clear_pending_guarded(a.repo, a.pr, a.force)
            if refusal:
                print(json.dumps(refusal, indent=1))
                sys.exit(3)
            if cleared.get("deleted"):
                report["deleted_pending"] = cleared["deleted"]
        try:
            require_head(a.repo, a.pr, a.commit)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            restore_draft(a.repo, a.pr, cleared)
            raise
        save_verdict(a.repo, a.pr, verdict_record(verdict, a.commit, diff, report))
        print(json.dumps({**report, "complete": True, "note": "no findings; nothing created"}, indent=1))
        return

    cleared = None
    if a.replace_pending:
        # Refuse (and print the report) BEFORE posting anything new — never
        # discard a pending review that isn't fully ours to begin with.
        cleared, refusal = clear_pending_guarded(a.repo, a.pr, a.force)
        if refusal:
            print(json.dumps(refusal, indent=1))
            sys.exit(3)
        if cleared.get("deleted"):
            report["deleted_pending"] = cleared["deleted"]

    try:
        require_head(a.repo, a.pr, a.commit)
        review = post_review(a.repo, a.pr, a.commit, staged, body)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        # Never retry uncertain publication or reinterpret anchors at a new
        # head. Retain a local recovery snapshot of any replaced owned draft.
        restore_draft(a.repo, a.pr, cleared)
        raise
    remember_posted(a.repo, a.pr, review, staged, review.get("body") or body)

    report.update({"complete": not omitted, "review_created": True, "review_id": review["id"],
                   "state": review["state"], "staged": len(staged), "carried": len(carried)})
    if not omitted:
        save_verdict(a.repo, a.pr, verdict_record(verdict, a.commit, diff, report))
    print(json.dumps(report, indent=1))


def verdict_record(verdict, head, diff, report):
    record = {"verdict": verdict, "head": head, "diff": diff, "at": int(time.time())}
    if report.get("verdict_override"):
        record["override"] = report["verdict_override"]
    # Per-file coverage at this head, which a later `--since` pass carries for
    # every file whose patch is unchanged. watch_review records through here
    # too, so a watcher-recorded head carries the same evidence.
    if isinstance(report.get("patches"), dict):
        record["patches"] = report["patches"]
    if isinstance(report.get("coverage"), dict) and report["coverage"].get("carried_from"):
        record["carried_from"] = report["coverage"]["carried_from"]
    return record


COMMANDS = {
    "map": cmd_map,
    "extract": cmd_extract,
    "pending": cmd_pending,
    "clear-pending": cmd_clear_pending,
    "threads": cmd_threads,
    "resolve-thread": cmd_resolve_thread,
    "reply": cmd_reply,
    "stage": cmd_stage,
    "verdict": cmd_verdict,
    "show": cmd_show,
    "delta": cmd_delta,
}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        sp = sub.add_parser(name)
        sp.add_argument("-R", "--repo", required=True, help="OWNER/REPO of the PR's base repo")
        sp.add_argument("-n", "--pr", required=True, type=int)
        if name == "clear-pending":
            sp.add_argument("--force", action="store_true",
                             help="delete even if it holds comments not staged by this script")
        if name in ("map", "extract", "resolve-thread", "reply", "verdict", "delta"):
            sp.add_argument("--commit", required=True, help="full reviewed head SHA")
        if name == "extract":
            sp.add_argument("paths", nargs="*")
        if name == "show":
            sp.add_argument("--commit", required=True, help="full SHA to read at (the reviewed head)")
            sp.add_argument("path", help="repository-relative file or directory; '' for the root")
            sp.add_argument("--lines", help="START:END, 1-based and inclusive; either side optional")
        if name == "delta":
            sp.add_argument("--since", required=True, help="full SHA of the earlier completely reviewed head")
        if name == "threads":
            sp.add_argument("--all", action="store_true", help="include resolved threads")
        if name == "resolve-thread":
            sp.add_argument("--evidence-file", required=True, help="JSON: thread_id, head_sha, explanation")
            sp.add_argument("--thread-id", required=True, help="PRRT_… thread node id")
            sp.add_argument("--dry-run", action="store_true")
        if name == "reply":
            sp.add_argument("--thread-id", required=True, help="PRRT_… thread node id")
            sp.add_argument("--body-file", required=True, help="file holding the reply body")
            sp.add_argument("--dry-run", action="store_true")
        if name == "stage":
            sp.add_argument("--commit", required=True, help="head SHA (headRefOid) to anchor comments to")
            sp.add_argument("--input", required=True, help="JSON file with the comments array")
            sp.add_argument("--replace-pending", action="store_true")
            sp.add_argument("--since", help="earlier completely reviewed head: carry coverage for files "
                                             "whose patch is unchanged since then")
            sp.add_argument("--force", action="store_true",
                             help="with --replace-pending, delete even if it holds "
                                  "comments not staged by this script")
            sp.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    try:
        a.repo = canonical_repo(a.repo)
        COMMANDS[a.cmd](a)
    except subprocess.TimeoutExpired:
        print("gh timed out; inspect remote state before retrying a mutation", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"gh failed: {e.stderr.strip() if e.stderr else e}", file=sys.stderr)
        sys.exit(1)
    except (KeyError, ValueError, OSError) as e:
        print(f"input error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
