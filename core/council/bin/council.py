#!/usr/bin/env python3
"""Council review support tool. See <repo>/core/council/DESIGN.md.

Tool-neutral: one engine serves Claude Code, Codex, OpenCode, and Cursor. State is
shared across tools (one STATE_ROOT outside every tool home and outside the repo), so
a review recorded by one host is visible to the others on the same repo+diff.

Subcommands:
  risk    [--json]                 Compute risk tier for the current repo's diff.
  hook                             Stop-hook handler (stdin: hook JSON). Fail-open.
  begin   --checkpoint impl|plan   Write an in-review marker (suppresses the nudge
                                   while a council is running). Cleared by `mark`.
  mark    --checkpoint impl|plan [--tier N] [--override --reason "..."]
                                   Record a reviewed/overridden marker for the current diff.
  ledger  (--entry '<json>' | --entry-file <path> | stdin)
                                   Append an entry to this project's ledger.
  hash                             Print the current diff hash.
  state-dir                        Print this project's state directory path.

Recursion guard: if $LEOS_COUNCIL_SEAT is set the process is a council seat/subagent;
`hook` exits 0 immediately so a seat can never be nudged to convene its own council.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")


def _repo_root():
    """<repo> located relative to this script (core/council/bin/council.py), so it
    resolves correctly even when invoked through a symlink from a tool home."""
    here = os.path.realpath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))


REPO_ROOT = _repo_root()
# Machine-local council config (disabledProjects) lives gitignored inside the clone.
CONFIG_PATH = os.environ.get(
    "LEOS_COUNCIL_CONFIG", os.path.join(REPO_ROOT, "local", "council", "config.json"))
# State (markers/ledger) lives OUTSIDE the repo and every tool home: shared across
# tools, and safe from `git clean` in the clone.
STATE_ROOT = os.environ.get(
    "LEOS_COUNCIL_STATE",
    os.path.join(os.environ.get("XDG_STATE_HOME", os.path.join(HOME, ".local", "state")),
                 "leos-agent", "council", "state"))
_SELF = os.path.realpath(__file__)

TIERS = ["skip", "low", "elevated", "high", "critical"]

MAX_PARSE_BYTES = 5 * 1024 * 1024   # cap diff parsing work
MAX_UNTRACKED_READ = 512 * 1024     # per-file content cap for hashing/scanning
MAX_UNTRACKED_FILES = 200
IN_REVIEW_TTL = 1800                # an in-review marker suppresses the nudge for 30 min

# --- Signals -----------------------------------------------------------------

RISK_PATH_RE = re.compile(
    r"(^|/)(auth|authn|authz|oauth|sso|acl|rbac|permissions?|security|migrations?|models?"
    r"|crypto|secrets?|payments?|billing)(/|\.|$)"
    r"|(^|/)\.github/workflows/"
    r"|(^|/)\.gitlab-ci\.yml$"
    r"|\.sql$"
    r"|(^|/)schema[^/]*$"
    r"|(^|/)(Dockerfile|docker-compose[^/]*)$",
    re.IGNORECASE,
)

# Docs/lockfiles/assets: never count toward risk on their own.
IGNORE_PATH_RE = re.compile(
    r"\.(md|mdx|txt|rst|adoc|svg|png|jpe?g|gif|webp|ico|lock)$"
    r"|(^|/)(LICENSE|NOTICE|CHANGELOG)[^/]*$"
    r"|(^|/)(pnpm-lock\.yaml|package-lock\.json|yarn\.lock|Cargo\.lock|poetry\.lock|uv\.lock|go\.sum)$",
    re.IGNORECASE,
)

TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|\.(test|spec)\.[a-z]+$", re.IGNORECASE)

DEP_FILE_RE = re.compile(
    r"(^|/)(package\.json|requirements[^/]*\.txt|pyproject\.toml|go\.mod|Cargo\.toml|Gemfile|composer\.json)$"
)

ENV_FILE_RE = re.compile(r"(^|/)\.env[^/]*$")

SECURITY_SYMBOL_RE = re.compile(
    r"\b(token|secret|password|passwd|credential|authoriz\w*|authenticat\w*|permission|csrf|jwt|cookie|session[_ ]?key)\b",
    re.IGNORECASE,
)

CONFIG_SURFACE_RE = re.compile(
    r"\b(cors|csp|content-security-policy|rate[_ -]?limit|redact\w*|allowlist|blocklist|origin)\b",
    re.IGNORECASE,
)

DATA_LOSS_RE = re.compile(
    r"\b(drop\s+(table|database|column)|truncate\s+table|delete\s+from|rm\s+-rf?)\b",
    re.IGNORECASE,
)

ASSERTION_RE = re.compile(r"\b(assert\w*|expect|should|toBe|toEqual|toThrow)\b")
EXPORT_RE = re.compile(r"^\s*export\s+(default\s+)?(async\s+)?(function|const|let|class|interface|type|enum)\b")
COMMENT_LINE_RE = re.compile(r"^\s*(#|//|/\*|\*|;|--)|^\s*$")


# --- Git helpers -------------------------------------------------------------

def _git(args, cwd):
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=20
        )
        return r.returncode, r.stdout, r.stderr
    except Exception:
        return 1, "", "git unavailable"


def resolve_base(cwd):
    """Return (base_ref_or_None, ambiguous). None base => unborn HEAD (use staged diff)."""
    code, _, _ = _git(["rev-parse", "--verify", "HEAD"], cwd)
    if code != 0:
        return None, True  # unborn HEAD (fresh repo, no commits)
    code, out, _ = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd)
    if code == 0 and out.strip():
        code2, mb, _ = _git(["merge-base", "HEAD", out.strip()], cwd)
        if code2 == 0 and mb.strip():
            return mb.strip(), False
    # Remote default branch (origin/HEAD), then common names.
    code, out, _ = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd)
    cands = [out.strip()] if code == 0 and out.strip() else []
    cands += ["origin/main", "origin/master", "origin/develop", "origin/trunk",
              "main", "master", "develop", "trunk"]
    for cand in cands:
        code, mb, _ = _git(["merge-base", "HEAD", cand], cwd)
        if code == 0 and mb.strip():
            return mb.strip(), False
    return "HEAD", True  # uncommitted-only view; ambiguous base


def _parse_name_status(raw):
    """Parse `git diff --name-status -z` output -> {path: status_char}."""
    out = {}
    parts = [p for p in raw.split("\0") if p != ""]
    i = 0
    while i < len(parts):
        status = parts[i][:1]
        if status in ("R", "C") and i + 2 < len(parts):
            out[parts[i + 2]] = status  # renames/copies: old, new
            i += 3
        elif i + 1 < len(parts):
            out[parts[i + 1]] = status
            i += 2
        else:
            break
    return out


def get_diff(cwd):
    """Return (diff_text, name_status_dict, untracked_paths, ambiguous)."""
    base, ambiguous = resolve_base(cwd)
    if base is None:
        code, diff, _ = _git(["diff", "--cached", "-M"], cwd)  # unborn HEAD: staged only
        if code != 0:
            diff = ""
        code, names, _ = _git(["diff", "--cached", "-M", "--name-status", "-z"], cwd)
    else:
        code, diff, _ = _git(["diff", "-M", base], cwd)
        if code != 0:
            code2, diff, _ = _git(["diff", "-M", "HEAD"], cwd)
            if code2 != 0:
                diff = ""
            ambiguous = True
            base = "HEAD"
        code, names, _ = _git(["diff", "-M", "--name-status", "-z", base], cwd)
    name_status = _parse_name_status(names) if code == 0 else {}
    code, out, _ = _git(["ls-files", "--others", "--exclude-standard"], cwd)
    untracked = [p for p in out.splitlines() if p.strip()] if code == 0 else []
    return diff, name_status, untracked, ambiguous


def _hash_all(cwd, diff_text, untracked):
    """Hash tracked diff + untracked file CONTENTS (bounded), so edits to untracked
    files invalidate markers."""
    h = hashlib.sha256()
    h.update(diff_text.encode("utf-8", "replace"))
    for p in sorted(untracked)[:MAX_UNTRACKED_FILES]:
        h.update(("\0" + p + "\0").encode("utf-8", "replace"))
        try:
            fp = os.path.join(cwd, p)
            size = os.path.getsize(fp)
            h.update(str(size).encode())
            with open(fp, "rb") as f:
                h.update(f.read(MAX_UNTRACKED_READ))
        except Exception:
            h.update(b"?")
    return h.hexdigest()[:16]


# --- Risk scoring ------------------------------------------------------------

def parse_diff(diff_text):
    """Return per-file {path: {"added": [...], "removed": [...]}}."""
    files = {}
    current = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            m = re.search(r" b/(.+)$", line)
            current = m.group(1) if m else None
            if current:
                files.setdefault(current, {"added": [], "removed": []})
        elif current and line.startswith("+") and not line.startswith("+++"):
            files[current]["added"].append(line[1:])
        elif current and line.startswith("-") and not line.startswith("---"):
            files[current]["removed"].append(line[1:])
    return files


def load_project_config(cwd):
    """Validated .council.json: bad values are dropped, never fatal."""
    cfg = {}
    try:
        p = os.path.join(cwd, ".council.json")
        if os.path.exists(p) and os.path.getsize(p) < 64 * 1024:
            with open(p) as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                cfg = raw
    except Exception:
        pass
    globs = cfg.get("riskGlobs")
    cfg["riskGlobs"] = [g for g in globs if isinstance(g, str) and len(g) < 200] \
        if isinstance(globs, list) else []
    th = cfg.get("thresholds")
    clean = {}
    if isinstance(th, dict):
        for k in ("smallLines", "smallFiles", "largeLines", "largeFiles"):
            v = th.get(k)
            if isinstance(v, int) and 0 < v < 1_000_000:
                clean[k] = v
    cfg["thresholds"] = clean
    return cfg


def compute_risk(cwd):
    """Return dict: tier, tier_index, reasons, hash, stats."""
    diff_text, name_status, untracked, ambiguous = get_diff(cwd)
    h = _hash_all(cwd, diff_text, untracked)

    truncated = len(diff_text) > MAX_PARSE_BYTES
    files = parse_diff(diff_text[:MAX_PARSE_BYTES])

    # Cross-check: files git names but header-parsing missed (crafted/quoted paths).
    unparsed = [n for n in name_status if n not in files]
    for p in unparsed:
        files[p] = {"added": [], "removed": []}

    for p in untracked[:MAX_UNTRACKED_FILES]:
        if p not in files:
            entry = {"added": [], "removed": []}
            try:
                fp = os.path.join(cwd, p)
                if os.path.getsize(fp) < MAX_UNTRACKED_READ:
                    with open(fp, errors="replace") as f:
                        entry["added"] = f.read().splitlines()
            except Exception:
                pass
            files[p] = entry

    cfg = load_project_config(cwd)
    extra_globs = cfg["riskGlobs"]
    th = cfg["thresholds"]
    small_lines = th.get("smallLines", 60)
    small_files = th.get("smallFiles", 3)
    large_lines = th.get("largeLines", 400)
    large_files = th.get("largeFiles", 10)

    code_files = {p: v for p, v in files.items() if not IGNORE_PATH_RE.search(p)}
    if not code_files:
        if ambiguous and (diff_text or untracked):
            return {"tier": "elevated", "tier_index": 2,
                    "reasons": ["ambiguous diff base with changes present — unknown floor"],
                    "hash": h, "stats": {"files": len(files)}}
        return {"tier": "skip", "tier_index": 0, "reasons": ["no code changes"],
                "hash": h, "stats": {"files": len(files)}}

    added = sum(len(v["added"]) for v in code_files.values())
    removed = sum(len(v["removed"]) for v in code_files.values())
    total = added + removed
    nfiles = len(code_files)
    workspaces = {p.split("/")[0] for p in code_files if "/" in p}

    reasons = []
    risk_paths = set()
    semantic = set()
    asserts_added_total = 0
    asserts_removed_total = 0

    for p, v in code_files.items():
        if RISK_PATH_RE.search(p) or any(fnmatch.fnmatch(p, g) for g in extra_globs):
            risk_paths.add(p)
        changed = v["added"] + v["removed"]
        blob = "\n".join(changed)
        if SECURITY_SYMBOL_RE.search(blob):
            semantic.add("security-symbols")
        if CONFIG_SURFACE_RE.search(blob):
            semantic.add("config-surface")
        if DATA_LOSS_RE.search(blob):
            semantic.add("data-loss")
        if any(EXPORT_RE.match(l) for l in changed):
            semantic.add("exported-api")
        if TEST_PATH_RE.search(p):
            asserts_removed_total += sum(1 for l in v["removed"] if ASSERTION_RE.search(l))
            asserts_added_total += sum(1 for l in v["added"] if ASSERTION_RE.search(l))
            if name_status.get(p) == "D":  # actual file deletion, not a pure-deletion edit
                semantic.add("test-file-deleted")
        if DEP_FILE_RE.search(p) and any(not COMMENT_LINE_RE.match(l) for l in v["added"]):
            semantic.add("new-dependencies")
        if ENV_FILE_RE.search(p):
            semantic.add("env-surface")

    if asserts_removed_total > asserts_added_total:
        semantic.add("assertions-removed")

    deletion_heavy = removed > 2 * added and removed > 100
    tests_touched = any(TEST_PATH_RE.search(p) for p in code_files)
    is_small = total <= small_lines and nfiles <= small_files
    is_large = total > large_lines or nfiles > large_files or len(workspaces) > 2 or truncated

    # Tier decision
    tier = 1  # low
    if not is_small:
        tier = 2
        reasons.append(f"medium+ blast radius ({nfiles} files, {total} lines)")
    if deletion_heavy:
        tier = max(tier, 2)
        reasons.append(f"deletion-heavy ({removed} removed vs {added} added)")
    if "new-dependencies" in semantic or "env-surface" in semantic:
        tier = max(tier, 2)
    if not tests_touched and total > small_lines:
        tier = max(tier, 2)
        reasons.append("non-trivial change with no test changes")
    if semantic & {"assertions-removed", "test-file-deleted"}:
        tier = max(tier, 2)  # weakened test safety net is never "low"
    if unparsed:
        tier = max(tier, 2)
        reasons.append(f"unparseable diff paths (treated as risk): {unparsed[:3]}")
    if risk_paths:
        tier = max(tier, 3)
        reasons.append(f"risk paths: {sorted(risk_paths)[:5]}")
    if is_large:
        tier = max(tier, 3)
        reasons.append(f"large blast radius ({nfiles} files, {total} lines, {len(workspaces)} workspaces)")
    if "data-loss" in semantic:
        tier = max(tier, 3)
    # Security symbols inside an already-flagged risk path are intrinsic, not extra signal.
    distinct_semantic = semantic - ({"security-symbols"} if risk_paths else set())
    if len(distinct_semantic) >= 2:
        tier = max(tier, 3)
    if semantic:
        reasons.append(f"semantic signals: {sorted(semantic)}")
    if (risk_paths and is_large) \
            or ("exported-api" in semantic and is_large) \
            or ("data-loss" in semantic and (risk_paths or is_large)):
        tier = 4
        reasons.append("critical combination (risk paths / public API / data-loss × blast radius)")
    if ambiguous and tier < 4:
        tier += 1
        reasons.append("ambiguous diff base (escalated one tier)")
    if truncated:
        reasons.append("diff exceeded parse cap (treated as large)")

    if not reasons:
        reasons.append(f"small isolated change ({nfiles} files, {total} lines)")

    return {"tier": TIERS[tier], "tier_index": tier, "reasons": reasons, "hash": h,
            "stats": {"files": nfiles, "added": added, "removed": removed,
                      "workspaces": sorted(workspaces)}}


# --- State -------------------------------------------------------------------

def project_root(cwd):
    code, out, _ = _git(["rev-parse", "--show-toplevel"], cwd)
    root = out.strip() if code == 0 and out.strip() else cwd
    return os.path.realpath(root)


def project_slug(cwd):
    root = project_root(cwd)
    base = re.sub(r"[^A-Za-z0-9]+", "-", os.path.basename(root)).strip("-") or "repo"
    digest = hashlib.sha256(root.encode("utf-8", "replace")).hexdigest()[:10]
    return f"{base}-{digest}", root


def state_dir(cwd):
    slug, root = project_slug(cwd)
    d = os.path.join(STATE_ROOT, slug)
    os.makedirs(os.path.join(d, "markers"), exist_ok=True)
    os.makedirs(os.path.join(d, "tmp"), exist_ok=True)
    rootfile = os.path.join(d, "root")
    if not os.path.exists(rootfile):
        with open(rootfile, "w") as f:
            f.write(root)
    return d


def marker_path(cwd, h):
    return os.path.join(state_dir(cwd), "markers", f"{h}.json")


def read_marker(cwd, h):
    try:
        with open(marker_path(cwd, h)) as f:
            return json.load(f)
    except Exception:
        return None


def write_marker(cwd, h, data):
    data = {"hash": h, "ts": int(time.time()), **data}
    with open(marker_path(cwd, h), "w") as f:
        json.dump(data, f, indent=2)
    return data


def append_ledger(cwd, entry):
    p = os.path.join(state_dir(cwd), "ledger.jsonl")
    entry = {"ts": int(time.time()), **entry}
    with open(p, "a") as f:
        f.write(json.dumps(entry) + "\n")


# --- Subcommands -------------------------------------------------------------

def cmd_risk(args):
    r = compute_risk(os.getcwd())
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"{r['tier']} ({r['tier_index']})")
        for reason in r["reasons"]:
            print(f"  - {reason}")
    return 0


def cmd_hash(_args):
    cwd = os.getcwd()
    diff_text, _, untracked, _ = get_diff(cwd)
    print(_hash_all(cwd, diff_text, untracked))
    return 0


def cmd_state_dir(_args):
    print(state_dir(os.getcwd()))
    return 0


def cmd_root(_args):
    """Print the leos-agent clone root (self-located), so the council skill can find
    local/seats.<host>.json and core/council/prompts regardless of the tool home."""
    print(REPO_ROOT)
    return 0


def cmd_begin(args):
    """Write an in-review marker so the Stop hook doesn't nudge while a council runs."""
    cwd = os.getcwd()
    diff_text, _, untracked, _ = get_diff(cwd)
    h = _hash_all(cwd, diff_text, untracked)
    data = write_marker(cwd, h, {"status": "in-review", "checkpoint": args.checkpoint})
    append_ledger(cwd, {"type": "begin", **data})
    print(f"in-review: {h}")
    return 0


def cmd_mark(args):
    cwd = os.getcwd()
    diff_text, _, untracked, _ = get_diff(cwd)
    h = _hash_all(cwd, diff_text, untracked)
    status = "overridden" if args.override else "reviewed"
    if args.override and not args.reason:
        print("--override requires --reason", file=sys.stderr)
        return 1
    data = write_marker(cwd, h, {
        "status": status,
        "checkpoint": args.checkpoint,
        "tier": args.tier,
        "reason": args.reason or "",
    })
    append_ledger(cwd, {"type": "marker", **data})
    print(f"marked {status}: {h}")
    return 0


def cmd_ledger(args):
    raw = None
    if args.entry_file:
        try:
            with open(args.entry_file) as f:
                raw = f.read()
        except Exception as e:
            print(f"cannot read entry file: {e}", file=sys.stderr)
            return 1
    elif args.entry:
        raw = args.entry
    else:
        raw = sys.stdin.read()
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"invalid JSON: {e}", file=sys.stderr)
        return 1
    entries = entry if isinstance(entry, list) else [entry]
    for e in entries:
        append_ledger(os.getcwd(), e)
    print(f"ok ({len(entries)} entries)")
    return 0


NUDGE_EXIT = 42  # distinctive: python/argparse startup failures use 1/2, shell uses 126/127.
                 # The host's Stop-hook wrapper maps 42 -> 2 (blocking nudge) and everything
                 # else -> 0, so no interpreter/script failure can ever masquerade as a nudge.


def cmd_hook(_args):
    """Stop hook. Exit 0 = allow stop; exit NUDGE_EXIT = nudge (stderr shown to the model).
    FAIL OPEN on every error path — never break the user's flow."""
    # Recursion guard: a council seat/subagent must never be nudged to convene its own
    # council. This env var is set by the council skill when it dispatches seats, and is
    # inherited by any hook the seat's own CLI fires.
    if os.environ.get("LEOS_COUNCIL_SEAT"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return 0
    try:
        root = project_root(cwd)
        if os.path.exists(os.path.join(root, ".council-off")):
            return 0
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                gcfg = json.load(f)
            if root in gcfg.get("disabledProjects", []):
                return 0

        code, _, _ = _git(["rev-parse", "--git-dir"], cwd)
        if code != 0:
            return 0

        risk = compute_risk(cwd)
        if risk["tier_index"] < 2:  # skip/low pass silently
            return 0

        h = risk["hash"]
        marker = read_marker(cwd, h)
        # A council currently in progress for this exact diff suppresses the nudge.
        if marker and marker.get("status") == "in-review" \
                and (int(time.time()) - marker.get("ts", 0)) < IN_REVIEW_TTL:
            return 0
        # Only an IMPL-checkpoint review/override clears the impl nudge (plan markers don't).
        if marker and marker.get("status") in ("reviewed", "overridden") \
                and marker.get("checkpoint") == "impl":
            return 0

        nudges = (marker or {}).get("nudges", 0)
        if nudges >= 2:
            return 0  # loop guard
        # Increment the nudge counter WITHOUT clobbering an existing marker's other fields
        # (e.g. a plan-checkpoint marker must keep counting or the loop guard never engages).
        merged = {**(marker or {"status": "nudged"}), "nudges": nudges + 1}
        merged.pop("ts", None)
        merged.pop("hash", None)
        write_marker(cwd, h, merged)

        reasons = "; ".join(risk["reasons"][:3])
        sys.stderr.write(
            f"[council] This diff scores '{risk['tier']}' risk ({reasons}) and has no council "
            f"review marker. Before finishing: EITHER run the council implementation checkpoint "
            f"(invoke the 'council' skill with checkpoint=impl), OR — if review is genuinely "
            f"unwarranted — record a logged override:\n"
            f"  python3 {_SELF} mark --checkpoint impl --override --reason \"<why>\"\n"
            f"If you are running as a council seat or subagent, ignore this nudge entirely — "
            f"do not convene a council or write an override marker. "
            f"Overrides are logged and surfaced to the developer. This nudge does not repeat "
            f"more than twice for the same diff.\n"
        )
        return NUDGE_EXIT
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser(prog="council.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("risk")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_risk)

    p = sub.add_parser("hash")
    p.set_defaults(fn=cmd_hash)

    p = sub.add_parser("state-dir")
    p.set_defaults(fn=cmd_state_dir)

    p = sub.add_parser("root")
    p.set_defaults(fn=cmd_root)

    p = sub.add_parser("begin")
    p.add_argument("--checkpoint", choices=["impl", "plan"], required=True)
    p.set_defaults(fn=cmd_begin)

    p = sub.add_parser("mark")
    p.add_argument("--checkpoint", choices=["impl", "plan"], required=True)
    p.add_argument("--tier", default="")
    p.add_argument("--override", action="store_true")
    p.add_argument("--reason", default="")
    p.set_defaults(fn=cmd_mark)

    p = sub.add_parser("ledger")
    p.add_argument("--entry", default="")
    p.add_argument("--entry-file", default="")
    p.set_defaults(fn=cmd_ledger)

    p = sub.add_parser("hook")
    p.set_defaults(fn=cmd_hook)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
