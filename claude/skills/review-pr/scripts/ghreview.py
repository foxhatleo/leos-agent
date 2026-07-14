#!/usr/bin/env python3
"""ghreview: helpers for staging PENDING GitHub PR reviews via gh.

Subcommands:
  map      -R OWNER/REPO -n PR                    JSON: per-file addressable-line ranges + flags
  extract  -R OWNER/REPO -n PR [PATH ...]         unified patches for the given files (all if none)
  pending  -R OWNER/REPO -n PR                    id of the current user's PENDING review, if any
  stage    -R OWNER/REPO -n PR --commit SHA --input FILE [--replace-pending] [--dry-run]
           validate comments against the diff, then create ONE pending review
           (payload deliberately has NO "event" field -> review stays PENDING)

stage --input file: {"comments": [{"path", "line", "side", "body",
                                   "start_line"?, "start_side"?}, ...]}
line = absolute line number in the new file for side RIGHT (old file for LEFT).
Off-diff lines are snapped to the nearest addressable line in the same hunk,
or dropped (reported on stderr) — one bad line would 422 the entire review.

Exit codes: 0 success; 1 API failure after retry; 2 usage/input error.
"""
import argparse
import json
import re
import subprocess
import sys

GENERATED_PATTERNS = [
    r"(^|/)package-lock\.json$", r"(^|/)yarn\.lock$", r"(^|/)pnpm-lock\.yaml$",
    r"(^|/)Cargo\.lock$", r"(^|/)Gemfile\.lock$", r"(^|/)poetry\.lock$",
    r"(^|/)uv\.lock$", r"(^|/)go\.sum$", r"(^|/)composer\.lock$",
    r"\.min\.(js|css)$", r"\.(map|snap)$", r"\.pb\.(go|py|rb|java)$", r"_pb2\.py$",
    r"(^|/)(dist|build|vendor|node_modules|__snapshots__)/", r"\.generated\.",
]
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SNAP_TOLERANCE = 3  # lines outside a hunk boundary still snapped into it


def gh(args, payload=None):
    """Run gh, return stdout. Raises CalledProcessError with stderr attached."""
    proc = subprocess.run(
        ["gh"] + args,
        input=payload,
        capture_output=True,
        text=True,
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
    """Return an addressable line for (side, line), or None to drop."""
    lines = diffmap["right" if side == "RIGHT" else "left"]
    if line in lines:
        return line
    key = "r" if side == "RIGHT" else "l"
    for hunk in diffmap["hunks"]:
        start, end = hunk[key]
        if start - SNAP_TOLERANCE <= line <= end + SNAP_TOLERANCE:
            in_hunk = [n for n in lines if start <= n <= end]
            if in_hunk:
                return min(in_hunk, key=lambda n: abs(n - line))
    return None


def validate_comments(comments, maps):
    staged, snapped, dropped = [], [], []
    for c in comments:
        path, body = c.get("path"), c.get("body", "").strip()
        side = c.get("side", "RIGHT")
        line = c.get("line")
        if not path or not body or not isinstance(line, int):
            dropped.append({**c, "reason": "missing path/line/body"})
            continue
        diffmap = maps.get(path)
        if diffmap is None:
            dropped.append({**c, "reason": "file not in diff (or binary/no patch)"})
            continue
        new_line = snap_line(diffmap, side, line)
        if new_line is None:
            dropped.append({**c, "reason": f"line {line} ({side}) not addressable in any hunk"})
            continue
        entry = {"path": path, "line": new_line, "side": side, "body": body}
        # Multi-line ranges: keep only if the start anchors cleanly before the
        # end on the same side; otherwise degrade to a single-line comment.
        start = c.get("start_line")
        if isinstance(start, int):
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


def pending_review_id(repo, pr):
    out = gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate", "--jq", ".[]"])
    login = current_login()
    for line in out.splitlines():
        if not line.strip():
            continue
        review = json.loads(line)
        if review.get("state") == "PENDING" and review.get("user", {}).get("login") == login:
            return review["id"]
    return None


def post_review(repo, pr, commit, staged):
    payload = json.dumps({"commit_id": commit, "comments": staged})  # no "event" -> PENDING
    out = gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "--method", "POST", "--input", "-"], payload)
    return json.loads(out)


def cmd_map(a):
    files = fetch_files(a.repo, a.pr)
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
        "files": report,
        "reviewable_files": len(reviewable),
        "reviewable_lines": sum(f["additions"] + f["deletions"] for f in reviewable),
    }, indent=1))


def cmd_extract(a):
    wanted = set(a.paths)
    for f in fetch_files(a.repo, a.pr):
        if wanted and f["filename"] not in wanted:
            continue
        if f.get("patch"):
            print(f"--- {f['filename']} ({f['status']}, +{f['additions']} -{f['deletions']})")
            print(f["patch"])
            print()


def cmd_pending(a):
    review_id = pending_review_id(a.repo, a.pr)
    if review_id:
        print(review_id)


def cmd_stage(a):
    with open(a.input) as fh:
        data = json.load(fh)
    comments = data["comments"] if isinstance(data, dict) else data
    if not comments:
        print(json.dumps({"staged": 0, "note": "no comments provided; nothing created"}))
        return

    files = fetch_files(a.repo, a.pr)
    staged, snapped, dropped = validate_comments(comments, build_maps(files))
    report = {"staged": len(staged), "snapped": snapped, "dropped": dropped}
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
        review_id = pending_review_id(a.repo, a.pr)
        if review_id:
            gh(["api", f"repos/{a.repo}/pulls/{a.pr}/reviews/{review_id}", "--method", "DELETE"])
            report["deleted_pending"] = review_id

    try:
        review = post_review(a.repo, a.pr, a.commit, staged)
    except subprocess.CalledProcessError as e:
        # Most likely a head moved under us or a line drifted: refresh and retry once.
        print(f"first attempt failed, revalidating against current head: {e.stderr.strip()}", file=sys.stderr)
        head = gh(["api", f"repos/{a.repo}/pulls/{a.pr}", "-q", ".head.sha"]).strip()
        files = fetch_files(a.repo, a.pr)
        staged, snapped2, dropped2 = validate_comments(staged, build_maps(files))
        report["snapped"] += snapped2
        report["dropped"] += dropped2
        if not staged:
            print(json.dumps({**report, "note": "nothing left to stage after revalidation"}))
            sys.exit(1)
        try:
            review = post_review(a.repo, a.pr, head, staged)
        except subprocess.CalledProcessError as e2:
            print(f"GitHub rejected the review again: {e2.stderr.strip()}", file=sys.stderr)
            sys.exit(1)

    report.update({"review_id": review["id"], "state": review["state"], "staged": len(staged)})
    print(json.dumps(report, indent=1))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("map", "extract", "pending", "stage"):
        sp = sub.add_parser(name)
        sp.add_argument("-R", "--repo", required=True, help="OWNER/REPO of the PR's base repo")
        sp.add_argument("-n", "--pr", required=True, type=int)
        if name == "extract":
            sp.add_argument("paths", nargs="*")
        if name == "stage":
            sp.add_argument("--commit", required=True, help="head SHA (headRefOid) to anchor comments to")
            sp.add_argument("--input", required=True, help="JSON file with the comments array")
            sp.add_argument("--replace-pending", action="store_true")
            sp.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    try:
        {"map": cmd_map, "extract": cmd_extract, "pending": cmd_pending, "stage": cmd_stage}[a.cmd](a)
    except subprocess.CalledProcessError as e:
        print(f"gh failed: {e.stderr.strip() if e.stderr else e}", file=sys.stderr)
        sys.exit(1)
    except (KeyError, ValueError, OSError) as e:
        print(f"input error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
