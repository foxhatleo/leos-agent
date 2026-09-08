#!/usr/bin/env python3
"""ghreview: helpers for staging PENDING GitHub PR reviews via gh.

Subcommands:
  map            -R OWNER/REPO -n PR              JSON: per-file addressable-line ranges + flags
  extract        -R OWNER/REPO -n PR [PATH ...]   unified patches for the given files (all if none)
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
  stage          -R OWNER/REPO -n PR --commit SHA --input FILE [--replace-pending] [--dry-run]
                 validate comments against the diff, then create ONE pending review
                 (payload deliberately has NO "event" field -> review stays PENDING)

stage --input file: {"comments": [{"path", "line", "side", "body",
                                   "start_line"?, "start_side"?}, ...]}
line = absolute line number in the new file for side RIGHT (old file for LEFT).
Off-diff lines are snapped to the nearest addressable line in the same hunk,
or dropped (reported on stderr) — one bad line would 422 the entire review.

Exit codes: 0 success; 1 API failure after retry; 2 usage/input error;
3 refused — a pending review being cleared (clear-pending, or stage
--replace-pending) contains comments not staged by this script; pass --force
to discard them anyway.
"""
import argparse
import json
import re
import subprocess
import hashlib
import os
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

GENERATED_PATTERNS = [
    r"(^|/)package-lock\.json$", r"(^|/)yarn\.lock$", r"(^|/)pnpm-lock\.yaml$",
    r"(^|/)Cargo\.lock$", r"(^|/)Gemfile\.lock$", r"(^|/)poetry\.lock$",
    r"(^|/)uv\.lock$", r"(^|/)go\.sum$", r"(^|/)composer\.lock$",
    r"\.min\.(js|css)$", r"\.(map|snap)$", r"\.pb\.(go|py|rb|java)$", r"_pb2\.py$",
    r"(^|/)(dist|build|vendor|node_modules|__snapshots__)/", r"\.generated\.",
]
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SNAP_TOLERANCE = 3  # lines outside a hunk boundary still snapped into it
SNAP_MAX_DISTANCE = 10  # beyond this from the requested line, drop instead of snapping

MARKER = "<!-- leos-agent:review-pr -->"

# The reviewer's procedure caps a review at 15 comments; this is the script's
# own backstop well above it, so a reviewer talked past its cap by a hostile
# diff still cannot blanket a pull request.
MAX_STAGE_COMMENTS = 50


def _mark(body):
    """Tag a comment body as tool-created, so clear-pending can tell it apart
    from anything Leo hand-drafted into the same pending review."""
    body = (body or "").rstrip()
    return body if MARKER in body else f"{body}\n\n{MARKER}"


def gh(args, payload=None):
    """Run gh, return stdout. Raises CalledProcessError with stderr attached."""
    proc = subprocess.run(
        ["gh"] + args,
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, proc.stdout, proc.stderr)
    return proc.stdout


def fetch_files(repo, pr):
    """List PR files as dicts. --paginate + --jq '.[]' yields NDJSON."""
    out = gh(["api", f"repos/{repo}/pulls/{pr}/files", "--paginate", "--jq", ".[]"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def is_generated(path):
    return any(re.search(p, path) for p in GENERATED_PATTERNS)


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

    for line in patch.splitlines():
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


def validate_comments(comments, maps):
    staged, snapped, dropped = [], [], []
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
        # end on the same side; otherwise degrade to a single-line comment.
        start = c.get("start_line")
        if isinstance(start, int) and not isinstance(start, bool):
            start_side = c.get("start_side", side)
            snapped_start = snap_line(maps[path], start_side, start)
            if snapped_start is not None and snapped_start < new_line and start_side == side:
                entry["start_line"] = snapped_start
                entry["start_side"] = start_side
        if new_line != line:
            snapped.append({"path": path, "from": line, "to": new_line})
        staged.append(entry)
    return staged, snapped, dropped


def current_login():
    return gh(["api", "user", "-q", ".login"]).strip()


def graphql(query, variables):
    """Run a GraphQL query/mutation via gh. Int/bool variables go through -F (typed)."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # bool before int: bool is a subclass of int, so this order matters.
        if isinstance(value, bool):
            args += ["-F", f"{key}={str(value).lower()}"]
        elif isinstance(value, int):
            args += ["-F", f"{key}={value}"]
        else:
            args += ["-f", f"{key}={value}"]
    return json.loads(gh(args))


def pending_review(repo, pr):
    """The current user's PENDING review as {"id", "node_id"}, or None.

    REST node_id is the GraphQL PullRequestReview id (verified identical) —
    usable directly in mutations.
    """
    out = gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate", "--jq", ".[]"])
    login = current_login()
    for line in out.splitlines():
        if not line.strip():
            continue
        review = json.loads(line)
        if review.get("state") == "PENDING" and review.get("user", {}).get("login") == login:
            return review
    return None


def review_comments(repo, pr, review_id):
    """All comments on a (pending) review, oldest first."""
    out = gh(["api", f"repos/{repo}/pulls/{pr}/reviews/{review_id}/comments",
              "--paginate", "--jq", ".[]"])
    return [json.loads(l) for l in out.splitlines() if l.strip()]


def receipt_path(repo, pr, review_id):
    from state import _data_root
    key = hashlib.sha256(f"{repo}:{pr}:{review_id}".encode()).hexdigest()
    return Path(_data_root()) / "reviews" / (key + ".json")


def comment_content(comment):
    return {key: comment[key] for key in ("path", "line", "side", "start_line", "start_side", "body")
            if comment.get(key) is not None}


def review_fingerprint(body, comments):
    rows = sorted(json.dumps(comment_content(c), sort_keys=True) for c in comments)
    return hashlib.sha256(json.dumps([body or "", rows]).encode()).hexdigest()


def remember_review(repo, pr, review, comments, body=""):
    from state import atomic_write
    atomic_write(str(receipt_path(repo, pr, review["id"])), {
        "fingerprint": review_fingerprint(body, comments), "review_id": review["id"]})


def pending_snapshot(repo, pr, force=False):
    review = pending_review(repo, pr)
    if review is None:
        return None, None
    comments = review_comments(repo, pr, review["id"])
    try:
        receipt = json.loads(receipt_path(repo, pr, review["id"]).read_text())
    except (OSError, ValueError):
        receipt = {}
    owned = receipt.get("fingerprint") == review_fingerprint(review.get("body"), comments)
    # Replies cannot be reconstructed safely as new root comments on recovery.
    recoverable = not any(c.get("in_reply_to_id") for c in comments)
    if not force and (not owned or not recoverable):
        return None, {"refused": True, "reason": "pending review is unowned, edited, or contains replies; preserve it",
                      "review_id": review["id"], "total_count": len(comments)}
    return {"review": review, "comments": comments, "forced": not owned}, None


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


def pinned_files(repo, pr, commit):
    require_head(repo, pr, commit)
    files = fetch_files(repo, pr)
    require_head(repo, pr, commit)
    return files


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
              id author { login } body createdAt
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
        nodes { id author { login } body createdAt pullRequestReview { id state } }
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
        conn = graphql(THREADS_QUERY, variables)["data"]["repository"]["pullRequest"]["reviewThreads"]
        for thread in conn["nodes"]:
            comments = thread["comments"]
            seen = set()
            while comments.get("pageInfo", {}).get("hasNextPage"):
                after = comments["pageInfo"]["endCursor"]
                if not after or after in seen:
                    raise ValueError("thread comment pagination did not advance")
                seen.add(after)
                page = graphql(THREAD_COMMENTS_QUERY, {"thread": thread["id"], "cursor": after})["data"]["node"]["comments"]
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
    payload = json.dumps({"commit_id": commit, "comments": staged, "body": body})  # no "event" -> PENDING
    out = gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "--method", "POST", "--input", "-"], payload)
    return json.loads(out)


def cmd_map(a):
    files = pinned_files(a.repo, a.pr, a.commit)
    report = []
    for f in files:
        diffmap = parse_patch(f["patch"]) if f.get("patch") else None
        report.append({
            "path": f["filename"],
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "generated": is_generated(f["filename"]),
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
    }, indent=1))


def cmd_extract(a):
    wanted = set(a.paths)
    for f in pinned_files(a.repo, a.pr, a.commit):
        if wanted and f["filename"] not in wanted:
            continue
        if f.get("patch"):
            print(f"--- {f['filename']} ({f['status']}, +{f['additions']} -{f['deletions']})")
            print(f["patch"])
            print()


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
        review = {"id": created["id"], "node_id": created["node_id"]}
    require_head(a.repo, a.pr, a.commit)
    result = graphql(REPLY_MUTATION, {
        "thread": a.thread_id, "review": review["node_id"], "body": body,
    })
    print(json.dumps({
        "staged_reply": result["data"]["addPullRequestReviewThreadReply"]["comment"]["id"],
        "thread": a.thread_id,
        "pending_review": review["id"],
    }))


def cmd_stage(a):
    with open(a.input) as fh:
        data = json.load(fh)
    comments = data["comments"] if isinstance(data, dict) else data
    if not comments:
        require_head(a.repo, a.pr, a.commit)
        if a.replace_pending and not a.dry_run:
            _, refusal = clear_pending_guarded(a.repo, a.pr, a.force)
            if refusal:
                print(json.dumps(refusal, indent=1))
                sys.exit(3)
        print(json.dumps({"repo": a.repo, "pr": a.pr, "commit": a.commit, "complete": not a.dry_run, "staged": 0, "note": "no findings; nothing created"}))
        return
    if len(comments) > MAX_STAGE_COMMENTS:
        print(
            f"input error: {len(comments)} comments exceeds the cap of {MAX_STAGE_COMMENTS}; "
            "a review this wide should be narrowed, not staged",
            file=sys.stderr,
        )
        sys.exit(2)

    files = pinned_files(a.repo, a.pr, a.commit)
    staged, snapped, dropped = validate_comments(comments, build_maps(files))
    report = {"repo": a.repo, "pr": a.pr, "commit": a.commit, "complete": False, "staged": len(staged), "snapped": snapped, "dropped": dropped}
    for d in dropped:
        print(f"unstageable: {d.get('path')}:{d.get('line')} — {d['reason']}", file=sys.stderr)

    if a.dry_run:
        report["comments"] = staged
        print(json.dumps(report, indent=1))
        return
    if not staged:
        print(json.dumps({**report, "note": "all comments were unstageable; no review created"}))
        return

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
        review = post_review(a.repo, a.pr, a.commit, staged)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        # Never retry uncertain publication or reinterpret anchors at a new
        # head. Retain a local recovery snapshot of any replaced owned draft.
        if report.get("deleted_pending"):
            backup = cleared["backup"]
            print(f"replacement failed; prior draft is recoverable from {backup}", file=sys.stderr)
            try:
                previous = json.loads(Path(backup).read_text())
                if pending_review(a.repo, a.pr) is None:
                    old = previous["review"]
                    restored = post_review(a.repo, a.pr, old["commit_id"],
                                           [comment_content(c) for c in previous["comments"]], old.get("body") or "")
                    remember_review(a.repo, a.pr, restored, previous["comments"], old.get("body") or "")
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                print("automatic restoration unavailable; recovery snapshot retained", file=sys.stderr)
        raise
    remember_review(a.repo, a.pr, review, staged)

    report.update({"complete": not dropped, "review_id": review["id"], "state": review["state"], "staged": len(staged)})
    print(json.dumps(report, indent=1))


COMMANDS = {
    "map": cmd_map,
    "extract": cmd_extract,
    "pending": cmd_pending,
    "clear-pending": cmd_clear_pending,
    "threads": cmd_threads,
    "resolve-thread": cmd_resolve_thread,
    "reply": cmd_reply,
    "stage": cmd_stage,
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
        if name in ("map", "extract", "resolve-thread", "reply"):
            sp.add_argument("--commit", required=True, help="full reviewed head SHA")
        if name == "extract":
            sp.add_argument("paths", nargs="*")
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
            sp.add_argument("--force", action="store_true",
                             help="with --replace-pending, delete even if it holds "
                                  "comments not staged by this script")
            sp.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    try:
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
