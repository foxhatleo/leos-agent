#!/usr/bin/env python3
"""review_learnings: what the user changed in staged reviews before submitting them.

ghreview.py keeps an ownership receipt for each pending review it stages. With
this module the receipt also keeps, per staged comment, a small data-only row:
path, line, severity, category and a normalized pattern of the staged text.
When that review is later found submitted, its comments are read once and
compared with those rows. A staged comment that is gone was `deleted`; one at
the same anchor with other text was `rewritten`, or `downgraded` when its new
text starts with a lower severity label (`nit:`, `minor:`, `optional:` ...).

Each outcome becomes an entry in
$LEOS_AGENT_LOCAL_PATH/review-learnings/<owner>__<repo>.json (directory 0700,
atomic locked writes): action, severity, category, a path glob, the pattern
and a count. Submitted text is untrusted and only ever classified; nothing
from it is stored. Nothing here is loaded unless a reviewer lists it, and a
learning is advisory: it may justify omitting or downgrading a non-blocking
finding, never a blocking one, and a major one only by downgrading it (stage
refuses the rest).
"""
import contextlib
import fcntl
import hashlib
import json
import os
import posixpath
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SEVERITIES = ("nit", "minor", "major", "blocking")
RANK = {name: i for i, name in enumerate(SEVERITIES)}
ACTIONS = ("deleted", "rewritten", "downgraded")
SUPPRESSIONS = ("omitted", "downgraded")
CATEGORY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
ENTRY_ID_RE = re.compile(r"[0-9a-f]{12}")
REPO_RE = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
MAX_ENTRIES = 200
MAX_PROCESSED = 500
PATTERN_WORDS = 12
PATTERN_CHARS = 100
MAX_REASON_CHARS = 300
# Controls, bidi overrides and zero-width characters: never stored or echoed.
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# A leading label that says "this is not important": the submitted side of a
# downgrade. Lowercased before matching.
LABELS = (("nitpick", "nit"), ("nit", "nit"), ("minor", "minor"), ("optional", "minor"),
          ("suggestion", "minor"), ("non-blocking", "minor"), ("nonblocking", "minor"))
LABEL_RE = re.compile(r"^[\W_]*(%s)\b" % "|".join(re.escape(word) for word, _ in LABELS))


def clean(text, limit=None):
    text = " ".join(CONTROL.sub(" ", str(text or "")).split())
    return text[:limit] if limit else text


def text_severity(body):
    """The severity a comment's own leading label states, or None."""
    text = HTML_COMMENT.sub(" ", body or "")
    match = LABEL_RE.match(clean(text).lower())
    return dict(LABELS)[match.group(1)] if match else None


def pattern(body):
    """A short, lowercase shape of a finding, without code, links or numbers."""
    text = HTML_COMMENT.sub(" ", body or "")
    text = re.sub(r"```.*?```", " <code> ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", " <code> ", text)
    text = re.sub(r"https?://\S+", " <url> ", text)
    text = re.sub(r"\d+", "#", clean(text).lower())
    words = re.findall(r"<code>|<url>|[a-z#][a-z#'-]*", text)
    return " ".join(words[:PATTERN_WORDS])[:PATTERN_CHARS]


def path_glob(path):
    """The directory and extension of a path, as a glob: src/auth/*.py."""
    directory, name = posixpath.split(clean(path, 300))
    ext = posixpath.splitext(name)[1]
    return (directory + "/" if directory else "") + "*" + ext


def staged_meta(comment, entry):
    """The receipt row for one staged comment: input `comment`, staged `entry`.

    ValueError for a severity or category stage cannot keep, raised before
    anything is mutated.
    """
    severity = comment.get("severity")
    if severity is not None and severity not in SEVERITIES:
        raise ValueError("comment severity must be one of %s" % ", ".join(SEVERITIES))
    category = comment.get("category")
    if category is not None and not (isinstance(category, str) and CATEGORY_RE.fullmatch(category)):
        raise ValueError("comment category must be a short lowercase label such as error-handling")
    return {"path": entry["path"], "line": entry.get("line"), "side": entry.get("side"),
            "severity": severity or text_severity(entry.get("body")),
            "category": category, "pattern": pattern(entry.get("body"))}


def classify(rows, submitted, identity):
    """[{action, severity, category, path, pattern}] for staged rows the user changed.

    `rows` maps a staged comment's identity hash to its receipt row; `identity`
    hashes a submitted comment the same way. Unchanged comments match by hash.
    A changed one is paired with a submitted root comment on the same path at
    the same line, or at the position GitHub gave it while pending (a pending
    comment cannot be moved, so its anchor survives an edit), or with the only
    unmatched one on that path when it alone is unmatched there.
    """
    roots = [c for c in submitted if isinstance(c, dict) and not c.get("in_reply_to_id")]
    remaining = list(roots)
    changed = []
    for key in sorted(rows):
        row = rows[key]
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        hit = next((c for c in remaining if identity(c) == key), None)
        if hit is not None:
            remaining.remove(hit)
        else:
            changed.append(row)
    events = []
    for row in changed:
        same_path = [c for c in remaining if c.get("path") == row["path"]]
        pick = next((c for c in same_path
                     if (row.get("line") is not None and row["line"] in (c.get("original_line"), c.get("line")))
                     or (row.get("position") is not None and row["position"] == c.get("original_position"))), None)
        if pick is None and len(same_path) == 1 and sum(1 for r in changed if r["path"] == row["path"]) == 1:
            pick = same_path[0]
        if pick is None:
            action = "deleted"
        else:
            remaining.remove(pick)
            now = text_severity(pick.get("body"))
            before = row.get("severity")
            action = ("downgraded" if before in RANK and now in RANK and RANK[now] < RANK[before]
                      else "rewritten")
        events.append({"action": action, "severity": row.get("severity"), "category": row.get("category"),
                       "path": row["path"], "pattern": row.get("pattern") or ""})
    return events


def learnings_path(repo):
    from state import _data_root
    match = REPO_RE.fullmatch(repo or "")
    if not match or match.group(1) in (".", "..") or match.group(2) in (".", ".."):
        raise ValueError("-R must be OWNER/REPO")
    return Path(_data_root()) / "review-learnings" / ("%s__%s.json" % match.groups())


@contextlib.contextmanager
def _locked(path):
    os.makedirs(str(path.parent), mode=0o700, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load(repo):
    """The repo's learnings file as {"entries", "processed"}; empty when absent.

    ValueError when it exists but is not a learnings file, so nothing is
    silently thrown away; `forget --all` resets it.
    """
    path = learnings_path(repo)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"entries": [], "processed": []}
    except (OSError, ValueError) as error:
        raise ValueError("%s is unreadable (%s); fix it or run `learnings forget --all`" % (path, error))
    if (not isinstance(data, dict) or not isinstance(data.get("entries", []), list)
            or not isinstance(data.get("processed", []), list)):
        raise ValueError("%s is not a learnings file; fix it or run `learnings forget --all`" % path)
    data.setdefault("entries", [])
    data.setdefault("processed", [])
    return data


def _save(repo, data):
    from state import atomic_write
    atomic_write(str(learnings_path(repo)), data)


def entry_id(key):
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:12]


def record(repo, review_id, events, now=None):
    """Fold one submitted review's events into the file and mark it processed.

    Returns the ids of the entries touched. A review already processed adds
    nothing, so a second harvest of the same review never double-counts.
    """
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    path = learnings_path(repo)
    with _locked(path):
        data = load(repo)
        if review_id in data["processed"]:
            return []
        entries = {e.get("id"): e for e in data["entries"] if isinstance(e, dict)}
        touched = []
        for event in events:
            fields = {"action": event["action"], "severity": event.get("severity") or "unknown",
                      "category": event.get("category") or "general",
                      "path_glob": path_glob(event["path"]), "pattern": clean(event.get("pattern"), PATTERN_CHARS)}
            key = entry_id([fields[k] for k in ("action", "severity", "category", "path_glob", "pattern")])
            entry = entries.get(key)
            if entry is None:
                entry = entries[key] = dict(fields, id=key, count=0, first_seen=day)
            entry["count"] = int(entry.get("count") or 0) + 1
            entry["last_seen"] = day
            touched.append(key)
        ranked = sorted(entries.values(), key=lambda e: (-int(e.get("count") or 0), str(e.get("last_seen")),
                                                        str(e.get("id"))))
        data["entries"] = ranked[:MAX_ENTRIES]
        data["processed"] = (data["processed"] + [review_id])[-MAX_PROCESSED:]
        data["repo"] = repo
        _save(repo, data)
    return touched


def processed(repo):
    try:
        return set(load(repo)["processed"])
    except ValueError:
        return set()


def forget(repo, ids=None, everything=False):
    """Remove entries by id, or all of them. Processed reviews stay processed,
    so a forgotten lesson is not learned again from the same review."""
    path = learnings_path(repo)
    with _locked(path):
        if everything:
            try:
                kept = load(repo)["processed"]
            except ValueError:
                kept = []
            data = {"repo": repo, "entries": [], "processed": kept}
            removed = None
        else:
            data = load(repo)
            before = len(data["entries"])
            data["entries"] = [e for e in data["entries"] if not (isinstance(e, dict) and e.get("id") in ids)]
            removed = before - len(data["entries"])
        if path.exists() or everything:
            _save(repo, data)
    return removed


def check_suppressed(value, repo):
    """The stage input's `suppressed` list, validated; ValueError otherwise.

    Each entry names a finding the reviewer omitted or downgraded because of a
    learning: path, severity, action, the learning id and a reason. A blocking
    finding is never suppressed, a major one is only downgraded, and the
    learning must exist for this repo.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("suppressed must be a list")
    known = None
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each suppressed entry must be an object")
        if item.get("severity") == "blocking":
            raise ValueError("a blocking finding is never suppressed by a learning; stage it")
        if item.get("severity") not in SEVERITIES:
            raise ValueError("suppressed severity must be one of nit, minor, major")
        if item.get("action") not in SUPPRESSIONS:
            raise ValueError("suppressed action must be omitted or downgraded")
        if item["severity"] == "major" and item["action"] == "omitted":
            raise ValueError("a major finding may be downgraded by a learning, never omitted; stage it")
        path, reason, learning = item.get("path"), item.get("reason"), item.get("learning")
        if not (isinstance(path, str) and path.strip()):
            raise ValueError("suppressed entry needs the finding's path")
        if not (isinstance(reason, str) and reason.strip()):
            raise ValueError("suppressed entry needs a reason")
        if known is None:
            known = {e.get("id") for e in load(repo)["entries"] if isinstance(e, dict)}
        if learning not in known:
            raise ValueError("suppressed entry names no recorded learning for %s: %r" % (repo, learning))
        row = {"path": clean(path, 300), "severity": item["severity"], "action": item["action"],
               "learning": learning, "reason": clean(reason, MAX_REASON_CHARS)}
        if isinstance(item.get("line"), int) and not isinstance(item.get("line"), bool):
            row["line"] = item["line"]
        out.append(row)
    return out
