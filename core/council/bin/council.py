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
RISK_CACHE_TTL = 90                 # reuse a scored risk for repeated Stop events in a turn
MAX_NUDGES = 2                      # persistent loop guard: nudges per review streak
MAX_REARMS = 3                      # re-arm the guard at most this many times on genuinely new work

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

def _git(args, cwd, env=None):
    try:
        run_env = dict(os.environ, **env) if env else None
        r = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=20, env=run_env
        )
        return r.returncode, r.stdout, r.stderr
    except Exception:
        return 1, "", "git unavailable"


def _base_cache_path(cwd):
    return os.path.join(state_dir(cwd), "base-cache.json")


def resolve_base(cwd):
    """Return the merge-base ref to diff against, or None for an unborn HEAD (use staged diff).

    A repo with no upstream/remote is NOT ambiguous — diffing against HEAD (uncommitted changes)
    is a legitimate base, so no risk escalation is applied for it (see _score). Cached by HEAD SHA
    so the candidate-branch loop runs at most once per HEAD."""
    code, head, _ = _git(["rev-parse", "--verify", "HEAD"], cwd)
    if code != 0:
        return None  # unborn HEAD (fresh repo, no commits) — legitimate staged-diff base
    head = head.strip()
    try:
        with open(_base_cache_path(cwd)) as f:
            cached = json.load(f)
        if cached.get("head") == head and cached.get("base"):
            return cached["base"]
    except Exception:
        pass
    base = None
    code, out, _ = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd)
    if code == 0 and out.strip():
        code2, mb, _ = _git(["merge-base", "HEAD", out.strip()], cwd)
        if code2 == 0 and mb.strip():
            base = mb.strip()
    if base is None:
        # Remote default branch (origin/HEAD), then common names (remote and local).
        code, out, _ = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd)
        cands = [out.strip()] if code == 0 and out.strip() else []
        cands += ["origin/main", "origin/master", "origin/develop", "origin/trunk",
                  "main", "master", "develop", "trunk"]
        for cand in cands:
            code, mb, _ = _git(["merge-base", "HEAD", cand], cwd)
            if code == 0 and mb.strip():
                base = mb.strip()
                break
    if base is None:
        base = "HEAD"  # no upstream/default branch — score uncommitted changes; NOT escalated
    try:
        with open(_base_cache_path(cwd), "w") as f:
            json.dump({"head": head, "base": base}, f)
    except Exception:
        pass
    return base


def snapshot_tree(cwd):
    """Capture the current worktree (tracked mods + untracked, honoring exclude-standard) as a git
    tree SHA, using a DEDICATED per-process temp index so the user's real staging area is never
    touched and concurrent council.py invocations on the same (shared-state) repo don't collide on
    one index/lock. Returns the tree SHA, or None on any failure (callers fail open)."""
    idx = os.path.join(state_dir(cwd), "tmp", f"snap-{os.getpid()}.index")

    def _clean():
        for suffix in ("", ".lock"):
            try:
                os.remove(idx + suffix)
            except OSError:
                pass
    try:
        _clean()
        env = {"GIT_INDEX_FILE": idx}
        _git(["read-tree", "HEAD"], cwd, env)  # seed from HEAD; harmless failure on unborn HEAD
        _git(["add", "-A"], cwd, env)
        code, out, _ = _git(["write-tree"], cwd, env)
        _clean()
        return out.strip() if code == 0 and out.strip() else None
    except Exception:
        _clean()
        return None


def diff_trees(cwd, tree_a, tree_b):
    """Return (diff_text, name_status) between two tree-ish, or (None, {}) on failure."""
    code, diff, _ = _git(["diff", "-M", tree_a, tree_b], cwd)
    if code != 0:
        return None, {}
    code2, names, _ = _git(["diff", "-M", "--name-status", "-z", tree_a, tree_b], cwd)
    return diff, _parse_name_status(names) if code2 == 0 else {}


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


def _read_untracked(cwd, untracked):
    """Read untracked file contents ONCE (bounded) -> {path: (size, bytes) | None}, so hashing
    and scoring never double-read the same files."""
    contents = {}
    for p in sorted(untracked)[:MAX_UNTRACKED_FILES]:
        try:
            fp = os.path.join(cwd, p)
            size = os.path.getsize(fp)
            with open(fp, "rb") as f:
                contents[p] = (size, f.read(MAX_UNTRACKED_READ))
        except Exception:
            contents[p] = None
    return contents


def get_diff(cwd, base_tree=None):
    """Return (diff_text, name_status, untracked_paths, untracked_contents, undeterminable).

    base_tree given => DELTA mode: diff between that reviewed tree and the current worktree
    snapshot (untracked are already folded into the snapshot, so untracked/contents are empty).
    Else => merge-base (or staged, on unborn HEAD) mode. `undeterminable` is True ONLY when a git
    diff genuinely fails — never merely because there is no upstream/remote."""
    if base_tree is not None:
        cur = snapshot_tree(cwd)
        if cur is None:
            return "", {}, [], {}, True
        diff, ns = diff_trees(cwd, base_tree, cur)
        if diff is None:
            return "", {}, [], {}, True
        return diff, ns, [], {}, False
    base = resolve_base(cwd)
    undeterminable = False
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
                undeterminable = True  # genuinely couldn't diff
            base = "HEAD"
        code, names, _ = _git(["diff", "-M", "--name-status", "-z", base], cwd)
    name_status = _parse_name_status(names) if code == 0 else {}
    code, out, _ = _git(["ls-files", "--others", "--exclude-standard"], cwd)
    untracked = [p for p in out.splitlines() if p.strip()] if code == 0 else []
    untracked_contents = _read_untracked(cwd, untracked)
    return diff, name_status, untracked, untracked_contents, undeterminable


def _hash_all(cwd, diff_text, untracked, untracked_contents):
    """Hash tracked diff + untracked file CONTENTS (preloaded, bounded), so edits to untracked
    files invalidate markers."""
    h = hashlib.sha256()
    h.update(diff_text.encode("utf-8", "replace"))
    for p in sorted(untracked)[:MAX_UNTRACKED_FILES]:
        h.update(("\0" + p + "\0").encode("utf-8", "replace"))
        c = untracked_contents.get(p)
        if c is None:
            h.update(b"?")
        else:
            size, raw = c
            h.update(str(size).encode())
            h.update(raw)
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


def compute_risk(cwd, *, base_tree=None):
    """Return dict: tier, tier_index, reasons, hash, stats. Uncached. base_tree scores the DELTA
    against a reviewed tree (see snapshot_tree) instead of the merge-base."""
    diff_text, name_status, untracked, untracked_contents, undeterminable = get_diff(cwd, base_tree)
    h = _hash_all(cwd, diff_text, untracked, untracked_contents)
    cfg = load_project_config(cwd)
    return _score(diff_text, name_status, untracked, untracked_contents, undeterminable, cfg, h)


def cached_risk(cwd, *, base_tree=None):
    """compute_risk with a short-TTL per-project cache keyed by (diff hash, base_tree), so repeated
    Stop events in a single turn don't re-score. Fail-open to a fresh compute on any cache error."""
    try:
        diff_text, name_status, untracked, untracked_contents, undeterminable = get_diff(cwd, base_tree)
        h = _hash_all(cwd, diff_text, untracked, untracked_contents)
        key = f"{h}:{base_tree or ''}"
        cpath = os.path.join(state_dir(cwd), "cache.json")
        try:
            with open(cpath) as f:
                cache = json.load(f)
            if cache.get("key") == key and (int(time.time()) - cache.get("ts", 0)) < RISK_CACHE_TTL:
                return cache["risk"]
        except Exception:
            pass
        cfg = load_project_config(cwd)
        risk = _score(diff_text, name_status, untracked, untracked_contents, undeterminable, cfg, h)
        try:
            with open(cpath, "w") as f:
                json.dump({"key": key, "ts": int(time.time()), "risk": risk}, f)
        except Exception:
            pass
        return risk
    except Exception:
        try:
            return compute_risk(cwd, base_tree=base_tree)
        except Exception:
            return {"tier": "skip", "tier_index": 0, "reasons": ["risk computation failed"],
                    "hash": "", "stats": {}}


def _score(diff_text, name_status, untracked, untracked_contents, undeterminable, cfg, h):
    """Pure scoring over already-loaded diff/untracked data — NO IO — so cached_risk can reuse it."""
    truncated = len(diff_text) > MAX_PARSE_BYTES
    files = parse_diff(diff_text[:MAX_PARSE_BYTES])

    # Cross-check: files git names but header-parsing missed (crafted/quoted paths).
    unparsed = [n for n in name_status if n not in files]
    for p in unparsed:
        files[p] = {"added": [], "removed": []}

    for p in untracked[:MAX_UNTRACKED_FILES]:
        if p not in files:
            entry = {"added": [], "removed": []}
            c = untracked_contents.get(p)
            if c is not None:
                size, raw = c
                if size < MAX_UNTRACKED_READ:
                    entry["added"] = raw.decode("utf-8", "replace").splitlines()
            files[p] = entry

    extra_globs = cfg["riskGlobs"]
    th = cfg["thresholds"]
    small_lines = th.get("smallLines", 120)
    small_files = th.get("smallFiles", 5)
    large_lines = th.get("largeLines", 400)
    large_files = th.get("largeFiles", 10)

    code_files = {p: v for p, v in files.items() if not IGNORE_PATH_RE.search(p)}
    if not code_files:
        if undeterminable and (diff_text or untracked):
            return {"tier": "elevated", "tier_index": 2,
                    "reasons": ["undeterminable diff base with changes present — unknown floor"],
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
    if undeterminable and tier < 4:
        tier += 1
        reasons.append("undeterminable diff base (escalated one tier)")
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


# Fixed-name project pointers (independent of the churning diff hash): the reviewed baselines
# (baseline-<checkpoint>.json), the in-review flag (in-review.json), and the persistent nudge
# loop-guard (nudge-state.json). All fail-open.

def _read_pointer(cwd, name):
    try:
        with open(os.path.join(state_dir(cwd), name)) as f:
            return json.load(f)
    except Exception:
        return None


def _write_pointer(cwd, name, data):
    try:
        with open(os.path.join(state_dir(cwd), name), "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# --- Subcommands -------------------------------------------------------------

def cmd_risk(args):
    r = cached_risk(os.getcwd())
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"{r['tier']} ({r['tier_index']})")
        for reason in r["reasons"]:
            print(f"  - {reason}")
    return 0


def cmd_hash(_args):
    cwd = os.getcwd()
    diff_text, _, untracked, uc, _ = get_diff(cwd)
    print(_hash_all(cwd, diff_text, untracked, uc))
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
    diff_text, _, untracked, uc, _ = get_diff(cwd)
    h = _hash_all(cwd, diff_text, untracked, uc)
    data = write_marker(cwd, h, {"status": "in-review", "checkpoint": args.checkpoint})  # legacy
    _write_pointer(cwd, "in-review.json", {"checkpoint": args.checkpoint, "ts": int(time.time())})
    append_ledger(cwd, {"type": "begin", **data})
    print(f"in-review: {h}")
    return 0


def cmd_mark(args):
    cwd = os.getcwd()
    diff_text, _, untracked, uc, _ = get_diff(cwd)
    h = _hash_all(cwd, diff_text, untracked, uc)
    status = "overridden" if args.override else "reviewed"
    if args.override and not args.reason:
        print("--override requires --reason", file=sys.stderr)
        return 1
    data = write_marker(cwd, h, {  # legacy hash marker (cross-tool / back-compat)
        "status": status,
        "checkpoint": args.checkpoint,
        "tier": args.tier,
        "reason": args.reason or "",
    })
    # Reviewed-tree baseline pointer: what the hook diffs against to score follow-up deltas.
    code, head, _ = _git(["rev-parse", "HEAD"], cwd)
    _write_pointer(cwd, f"baseline-{args.checkpoint}.json", {
        "checkpoint": args.checkpoint, "status": status, "tier": args.tier,
        "reason": args.reason or "", "reviewed_tree": snapshot_tree(cwd),
        "head": head.strip() if code == 0 else "", "hash": h, "ts": int(time.time())})
    # A real review re-arms nothing: clear the persistent nudge guard for this checkpoint.
    ns = _read_pointer(cwd, "nudge-state.json") or {}
    ns[args.checkpoint] = {"count": 0, "ts": int(time.time())}
    _write_pointer(cwd, "nudge-state.json", ns)
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

        full = cached_risk(cwd)
        if full["tier_index"] < 2:  # skip/low pass silently
            return 0

        # A council currently running (any diff) suppresses the nudge.
        ir = _read_pointer(cwd, "in-review.json")
        if ir and (int(time.time()) - ir.get("ts", 0)) < IN_REVIEW_TTL:
            return 0

        # Delta-aware: if this branch has a reviewed baseline, score only the INCREMENT since it.
        # A trivial follow-up on top of a reviewed change must not re-trigger a full review.
        risk = full
        incremental = False
        base = _read_pointer(cwd, "baseline-impl.json")
        reviewed_tree = base.get("reviewed_tree") if base and base.get("status") in ("reviewed", "overridden") else None
        if reviewed_tree:
            # The tree object can be gc-pruned on a long-lived branch; if it no longer resolves the
            # delta would look falsely empty (skip) and silently suppress a warranted re-review, so
            # drop the baseline and fall through to full-risk instead.
            code, _, _ = _git(["cat-file", "-e", reviewed_tree + "^{tree}"], cwd)
            if code != 0:
                reviewed_tree = None
        if reviewed_tree:
            cur = snapshot_tree(cwd)
            if cur and cur == reviewed_tree:
                return 0  # nothing changed since the review
            if cur:
                delta = cached_risk(cwd, base_tree=reviewed_tree)
                if delta["tier_index"] < 2:
                    return 0  # trivial incremental change -> no re-nudge
                risk = delta
                incremental = True
            # cur is None (snapshot failed) -> fall through on `full` risk, still loop-guarded.

        # Persistent loop guard (project+checkpoint scoped): survives diff-hash churn across edits.
        ns = _read_pointer(cwd, "nudge-state.json") or {}
        slot = ns.get("impl") or {}
        count = slot.get("count", 0)
        anchor = slot.get("anchor_tree")
        rearms = slot.get("rearms", 0)
        if count >= MAX_NUDGES:
            # Re-arm once if there is genuinely NEW substantial work since the guard first tripped.
            rearmed = False
            if anchor and rearms < MAX_REARMS:
                cur2 = snapshot_tree(cwd)
                if cur2 and cur2 != anchor and cached_risk(cwd, base_tree=anchor)["tier_index"] >= 2:
                    count, rearms, anchor, rearmed = 0, rearms + 1, None, True
            if not rearmed:
                return 0
        if anchor is None:
            anchor = snapshot_tree(cwd)
        ns["impl"] = {"count": count + 1, "ts": int(time.time()), "anchor_tree": anchor,
                      "rearms": rearms}
        _write_pointer(cwd, "nudge-state.json", ns)

        reasons = "; ".join(risk["reasons"][:3])
        scope = ("the incremental change since your last council review" if incremental
                 else "this diff")
        sys.stderr.write(
            f"[council] {scope} scores '{risk['tier']}' risk ({reasons}) and has no fresh council "
            f"review marker. Before finishing: EITHER run the council implementation checkpoint "
            f"(invoke the 'council' skill with checkpoint=impl), OR — if review is genuinely "
            f"unwarranted — record a logged override:\n"
            f"  python3 {_SELF} mark --checkpoint impl --override --reason \"<why>\"\n"
            f"If you are running as a council seat or subagent, ignore this nudge entirely — "
            f"do not convene a council or write an override marker. "
            f"Overrides are logged and surfaced to the developer. This nudge does not repeat "
            f"more than {MAX_NUDGES} times per review.\n"
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
