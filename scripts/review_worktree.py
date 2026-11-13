#!/usr/bin/env python3
"""review_worktree: a detached worktree of a pull request head, and running a
command in it once the user said yes.

Subcommands:
  add    -R OWNER/REPO -n N -C REPO_DIR --commit SHA
         fetch refs/pull/N/head from REPO_DIR's origin (which must be
         OWNER/REPO), require it to be SHA, and check it out detached under
         <data-root>/review-worktrees/<owner>__<repo>/<N>-<sha12>, with hooks
         off and LFS smudging skipped. Idempotent. A worktree recorded for an
         older head of the same PR is removed first.
         JSON: {"path","commit","reused","cross_repository","symlinks":[...]}
  remove -R OWNER/REPO -n N [--all-heads]
         kill the PR's recorded process groups, remove its worktree and run
         files, drop its state. --all-heads also sweeps unrecorded <N>-*
         directories left by a crash. Nothing to remove is not an error.
         JSON: {"removed":[paths],"killed":[pgids],"refused":[paths]}
  list   [-R OWNER/REPO]
         JSON: {"worktrees":[{"repo","pr","path","commit","created"}]}
  run    -R OWNER/REPO -n N -C REPO_DIR --commit SHA --approved "TEXT"
         [--timeout 600] [--serve --port P] -- CMD...
         front end only, after the user's yes, quoting the user's words in
         --approved. Refuses a fork, a moved head and a closed PR. CMD runs
         in the worktree with an environment rebuilt from scratch (PATH,
         LANG, TERM, CI=1, a fresh HOME and TMPDIR, HOST/BIND=127.0.0.1,
         PORT with --serve, NPM_CONFIG_USERCONFIG=/dev/null): no tokens, no
         SSH agent, .env never sourced. Foreground JSON:
         {"exit","timed_out","log","tail"}; the tail is the last 200 lines
         with control characters shown escaped. --serve starts CMD in its own
         process group and returns at once: {"pgid","port","log"}.
         Every run is appended to the PR's executed[] in the state file.

A scrubbed environment is not a sandbox: CMD still runs as the user, with
the user's files and network. That is why it needs the user's yes and never
runs for a fork.

State: <data-root>/review-worktrees.json, {repo: {N: {path, commit,
repo_dir, pgids, created, executed}}}, written under state.py's lock with an
atomic replace. <data-root> is $LEOS_AGENT_LOCAL_PATH or ~/.leos-agent-local.

Python callers: remove_worktrees(repo, number=None) removes one PR's
worktrees (all of the repo's with number=None) and never raises for missing
state.

Exit codes: 0 ok (run: the command ran, whatever its own exit); 1 git or gh
failure; 2 usage or input error (including an --approved text nobody wrote);
3 refused (origin is not OWNER/REPO, no worktree at SHA, a path outside the
review-worktrees root); 4 head moved; 6 fork, or fork status unknown;
7 PR not open.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import state  # noqa: E402

try:
    import ghreview as _ghreview  # noqa: E402
except Exception:  # another tool's file; fall back to the local copies below
    _ghreview = None

STATE_NAME = "review-worktrees"
GH_TIMEOUT = 30
TAIL_LINES = 200
TAIL_MAX_CHARS = 16000
KILL_GRACE = 5.0
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
WORKTREE_NAME = re.compile(r"^(\d+)-[0-9a-f]{12}$")
# Variables that point git at a repository; inherited, they would override -C.
GIT_LOCATING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
                "GIT_PREFIX", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")
_PLACEHOLDER = re.compile(
    r"^\s*<[^<>]*>\s*$|^[\s.…_-]*$|^\W*(todo|tbd|text|approved|user'?s words|the user'?s words)\W*$",
    re.IGNORECASE)
_INVISIBLE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f­​-‏‪-‮"
                        "  ⁠-⁤⁦-⁩﻿]")


class Refused(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


def visible(text):
    if _ghreview is not None and hasattr(_ghreview, "visible"):
        return _ghreview.visible(text)
    return _INVISIBLE.sub(lambda m: ("\\u%04x" if ord(m.group()) > 0xff else "\\x%02x") % ord(m.group()), text)


def template_like(text):
    """An --approved text nobody wrote: empty, a placeholder, an example copied.

    ghreview's override pattern is reused, but not its length floor: the
    user's real answer is often a bare "yes"."""
    text = text or ""
    if not text.strip() or _PLACEHOLDER.search(text):
        return True
    pattern = getattr(_ghreview, "TEMPLATE_OVERRIDE", None) if _ghreview is not None else None
    return bool(pattern is not None and pattern.search(text.strip()))


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def worktrees_root():
    return os.path.join(state._data_root(), "review-worktrees")


def state_path():
    return os.path.join(state._data_root(), STATE_NAME + ".json")


def repo_dirname(repo):
    owner, name = repo.split("/", 1)
    return f"{owner}__{name}"


def check_repo(repo):
    if not REPO_RE.match(repo or "") or any(part in (".", "..") for part in repo.split("/")):
        raise Refused(2, f"not an OWNER/REPO: {repo!r}")


def inside_root(path):
    """True for <root>/<owner>__<repo>/<name> exactly, after resolving links."""
    root = os.path.realpath(worktrees_root())
    real = os.path.realpath(path)
    if not real.startswith(root + os.sep):
        return False
    parts = os.path.relpath(real, root).split(os.sep)
    return len(parts) == 2 and all(p not in ("", ".", "..") for p in parts)


def run_dir(path):
    """Logs and throwaway HOMEs for one worktree, beside it, not inside it."""
    return os.path.join(os.path.dirname(path), ".runs", os.path.basename(path))


def git_env(extra=None):
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCATING}
    env.update(extra or {})
    return env


def git(args, cwd=None, extra_env=None, check=True):
    cmd = ["git", "-c", "core.hooksPath=/dev/null"]
    if cwd:
        cmd += ["-C", cwd]
    proc = subprocess.run(cmd + args, capture_output=True, text=True, env=git_env(extra_env))
    if check and proc.returncode:
        raise Refused(1, f"git {' '.join(args)} failed: {visible(proc.stderr.strip())}")
    return proc


def gh(args):
    """Run gh, return stdout; tests replace this function."""
    proc = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=GH_TIMEOUT)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, proc.stdout, proc.stderr)
    return proc.stdout


def pr_view(repo, number, fields):
    """gh pr view's JSON for these fields, or None when gh fails."""
    try:
        data = json.loads(gh(["pr", "view", str(number), "-R", repo, "--json", ",".join(fields)]))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return data if isinstance(data, dict) else None


def origin_repo(url):
    """OWNER/REPO from an https, ssh:// or scp-style remote URL, else None."""
    url = (url or "").strip()
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]+/(.+)$", url) or re.match(r"^(?:[^@/:]+@)?[^/:]+:(?!//)(.+)$", url)
    if not m:
        return None
    path = m.group(1).rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path if REPO_RE.match(path) else None


def _load(lenient):
    path = state_path()
    try:
        return state.load(path)
    except SystemExit:
        if lenient:
            return {}
        raise Refused(1, f"{path} is corrupt; fix or delete it")


def _update(fn, lenient=False):
    """Read, change and atomically write the state under state.py's lock."""
    path = state_path()
    with state._locked(path):
        data = _load(lenient)
        result = fn(data)
        data = {k: v for k, v in data.items() if v}
        state.atomic_write(path, data)
    return result


def _ps_start(pid):
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else ""


def _group_alive(pgid):
    try:
        os.waitpid(-pgid, os.WNOHANG)  # reap our own children, so a zombie does not count
    except (ChildProcessError, OSError):
        pass
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_group(pgid, started=None):
    """SIGTERM a process group, SIGKILL after KILL_GRACE. False if it was gone,
    or if its leader's pid now belongs to a different process."""
    if not isinstance(pgid, int) or pgid <= 1 or not _group_alive(pgid):
        return False
    if started:
        current = _ps_start(pgid)
        if current and current != started:
            return False  # the pid was reused; not ours to kill
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + KILL_GRACE
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(20):
        if not _group_alive(pgid):
            break
        time.sleep(0.05)
    return True


def _drop_tree(path, repo_dir=None):
    """git worktree remove --force, then delete whatever is left. Guarded."""
    if not inside_root(path):
        return False
    if repo_dir and os.path.isdir(repo_dir):
        git(["worktree", "remove", "--force", path], cwd=repo_dir, check=False)
    elif os.path.isfile(os.path.join(path, ".git")):
        # Unrecorded: the worktree's .git file names its admin dir in the main repo.
        try:
            with open(os.path.join(path, ".git")) as fh:
                admin = fh.read().strip().split("gitdir:", 1)[-1].strip()
            common = os.path.dirname(os.path.dirname(admin))
            git(["--git-dir", common, "worktree", "remove", "--force", path], check=False)
        except OSError:
            pass
    if os.path.lexists(path):
        shutil.rmtree(path, ignore_errors=True)
    if repo_dir and os.path.isdir(repo_dir):
        git(["worktree", "prune"], cwd=repo_dir, check=False)
    runs = run_dir(path)
    if os.path.isdir(runs) and os.path.realpath(runs).startswith(os.path.realpath(worktrees_root()) + os.sep):
        shutil.rmtree(runs, ignore_errors=True)
    return not os.path.lexists(path)


def _remove_entry(entry, report):
    starts = entry.get("pgid_started") or {}
    for pgid in entry.get("pgids") or []:
        if kill_group(pgid, starts.get(str(pgid))):
            report["killed"].append(pgid)
    path = entry.get("path") or ""
    if not path:
        return
    if not inside_root(path):
        report["refused"].append(path)
        return
    existed = os.path.lexists(path)
    if existed or os.path.isdir(run_dir(path)):
        if _drop_tree(path, entry.get("repo_dir")) and existed:
            report["removed"].append(path)


def remove_worktrees(repo, number=None, all_heads=True):
    """Remove a PR's review worktrees (every PR's in repo with number=None).

    Kills recorded process groups, removes worktrees, drops state. Safe when
    nothing exists; refuses, without touching, any path outside the root."""
    report = {"removed": [], "killed": [], "refused": []}
    try:
        check_repo(repo)
    except Refused:
        return report
    repo_root = os.path.join(worktrees_root(), repo_dirname(repo))
    if not os.path.exists(state_path()) and not os.path.isdir(repo_root):
        return report
    try:
        key = None if number is None else str(int(number))
    except (TypeError, ValueError):
        return report

    def change(data):
        entries = data.get(repo) if isinstance(data.get(repo), dict) else {}
        for n in [n for n in entries if key is None or n == key]:
            entry = entries.pop(n)
            if isinstance(entry, dict):
                _remove_entry(entry, report)
        if entries:
            data[repo] = entries
        else:
            data.pop(repo, None)

    if os.path.exists(state_path()):
        _update(change, lenient=True)
    if all_heads and os.path.isdir(repo_root):
        for parent in (repo_root, os.path.join(repo_root, ".runs")):
            if not os.path.isdir(parent):
                continue
            for name in sorted(os.listdir(parent)):
                m = WORKTREE_NAME.match(name)
                if not m or (key is not None and m.group(1) != key):
                    continue
                path = os.path.join(repo_root, name)
                existed = os.path.lexists(path)
                if existed or os.path.isdir(run_dir(path)):
                    if _drop_tree(path) and existed and path not in report["removed"]:
                        report["removed"].append(path)
    return report


def _entry(repo, number):
    entry = (_load(lenient=True).get(repo) or {}).get(str(number))
    return entry if isinstance(entry, dict) else None


def head_of(path):
    proc = git(["rev-parse", "HEAD"], cwd=path, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def cmd_add(args):
    check_repo(args.repo)
    if not SHA_RE.match(args.commit):
        raise Refused(2, "--commit must be a full lowercase SHA")
    state.state_file(STATE_NAME)  # creates the data root 0700 on first use
    repo_dir = os.path.realpath(args.C)
    if git(["rev-parse", "--git-dir"], cwd=repo_dir, check=False).returncode:
        raise Refused(2, f"{repo_dir} is not a git repository")
    url = git(["config", "--get", "remote.origin.url"], cwd=repo_dir, check=False).stdout.strip()
    found = origin_repo(url)
    if found is None or found.lower() != args.repo.lower():
        raise Refused(3, f"origin of {repo_dir} is {visible(url) or 'unset'}, not {args.repo}")
    git(["fetch", "--no-tags", "origin", f"refs/pull/{args.number}/head"], cwd=repo_dir)
    fetched = git(["rev-parse", "FETCH_HEAD"], cwd=repo_dir).stdout.strip()
    if fetched != args.commit:
        raise Refused(4, f"head moved: PR #{args.number} is at {fetched}, not {args.commit}")

    path = os.path.join(worktrees_root(), repo_dirname(args.repo), f"{args.number}-{args.commit[:12]}")
    for d in (worktrees_root(), os.path.dirname(path)):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    if not inside_root(path):
        raise Refused(3, f"{path} resolves outside {worktrees_root()}")

    previous = _entry(args.repo, args.number)
    if previous and previous.get("commit") != args.commit:
        remove_worktrees(args.repo, args.number, all_heads=False)
        previous = None
    reused = os.path.isdir(path) and head_of(path) == args.commit
    if not reused:
        if os.path.lexists(path):
            _drop_tree(path, repo_dir)
        git(["worktree", "prune"], cwd=repo_dir, check=False)
        git(["worktree", "add", "--detach", path, args.commit], cwd=repo_dir,
            extra_env={"GIT_LFS_SKIP_SMUDGE": "1"})
        if head_of(path) != args.commit:
            _drop_tree(path, repo_dir)
            raise Refused(1, f"worktree at {path} is not at {args.commit}")

    tree = git(["ls-tree", "-r", "-z", args.commit], cwd=repo_dir).stdout
    symlinks = []
    for record in tree.split("\0"):
        meta, _, name = record.partition("\t")
        if meta.startswith("120000 "):
            symlinks.append(visible(name))

    def change(data):
        entries = data.setdefault(args.repo, {})
        old = entries.get(str(args.number)) if reused else None
        old = old if isinstance(old, dict) else {}
        entries[str(args.number)] = {
            "path": path, "commit": args.commit, "repo_dir": repo_dir,
            "pgids": old.get("pgids") or [], "pgid_started": old.get("pgid_started") or {},
            "created": old.get("created") or now(), "executed": old.get("executed") or []}

    _update(change)
    view = pr_view(args.repo, args.number, ["isCrossRepository"])
    cross = view.get("isCrossRepository") if view else None
    emit({"path": path, "commit": args.commit, "reused": reused,
          "cross_repository": cross if isinstance(cross, bool) else None, "symlinks": symlinks})
    return 0


def cmd_remove(args):
    check_repo(args.repo)
    report = remove_worktrees(args.repo, args.number, all_heads=args.all_heads)
    emit(report)
    return 3 if report["refused"] else 0


def cmd_list(args):
    if args.repo:
        check_repo(args.repo)
    rows = []
    for repo, entries in sorted(_load(lenient=True).items()):
        if (args.repo and repo != args.repo) or not isinstance(entries, dict):
            continue
        for n, e in sorted(entries.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            if isinstance(e, dict):
                rows.append({"repo": repo, "pr": int(n) if n.isdigit() else n, "path": e.get("path"),
                             "commit": e.get("commit"), "created": e.get("created")})
    emit({"worktrees": rows})
    return 0


def scrubbed_env(home, tmp, port=None):
    """The child's whole environment. Nothing else is inherited."""
    env = {"PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
           "LANG": os.environ.get("LANG") or "C.UTF-8",
           "TERM": os.environ.get("TERM") or "dumb",
           "CI": "1", "HOME": home, "TMPDIR": tmp,
           "HOST": "127.0.0.1", "BIND": "127.0.0.1",
           "NPM_CONFIG_USERCONFIG": "/dev/null"}
    if port is not None:
        env["PORT"] = str(port)
    return env


def tail_of(log):
    try:
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 512 * 1024))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    text = "\n".join(text.splitlines()[-TAIL_LINES:])
    text = visible(text)
    return text[-TAIL_MAX_CHARS:]


def cmd_run(args):
    check_repo(args.repo)
    cmd = list(args.cmd or [])
    if not cmd:
        raise Refused(2, "no command: put it after --")
    if not SHA_RE.match(args.commit):
        raise Refused(2, "--commit must be a full lowercase SHA")
    if template_like(args.approved):
        raise Refused(2, "--approved must quote the user's own answer, not a placeholder")
    if args.serve and args.port is None:
        raise Refused(2, "--serve needs --port")
    if args.port is not None and not 1024 <= args.port <= 65535:
        raise Refused(2, "--port must be 1024 to 65535")
    if args.timeout <= 0:
        raise Refused(2, "--timeout must be positive")

    entry = _entry(args.repo, args.number)
    path = (entry or {}).get("path") or ""
    if (not entry or entry.get("commit") != args.commit or not inside_root(path)
            or not os.path.isdir(path) or head_of(path) != args.commit
            or os.path.realpath(entry.get("repo_dir") or "") != os.path.realpath(args.C)):
        raise Refused(3, f"no review worktree at {args.commit[:12]} for {args.repo}#{args.number}; "
                         "run review_worktree.py add first")

    view = pr_view(args.repo, args.number, ["state", "isCrossRepository", "headRefOid"])
    if view is None or not isinstance(view.get("isCrossRepository"), bool):
        raise Refused(6, "cannot tell whether the PR comes from a fork; not running")
    if view["isCrossRepository"]:
        raise Refused(6, "the PR comes from a fork; its code never runs here")
    if view.get("state") != "OPEN":
        raise Refused(7, f"PR #{args.number} is {view.get('state') or 'not open'}")
    if view.get("headRefOid") != args.commit:
        raise Refused(4, f"head moved: PR #{args.number} is at {view.get('headRefOid')}, not {args.commit}")

    runs = run_dir(path)
    os.makedirs(runs, mode=0o700, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    home = tempfile.mkdtemp(prefix="home-", dir=runs)
    tmp = os.path.join(home, "tmp")
    os.mkdir(tmp, 0o700)
    log = os.path.join(runs, f"run-{stamp}.log")
    env = scrubbed_env(home, tmp, args.port if args.serve else None)
    record = {"cmd": cmd, "head": args.commit, "exit": None, "at": now(), "approved": args.approved}

    try:
        with open(log, "wb") as out:
            proc = subprocess.Popen(cmd, cwd=path, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as error:
        shutil.rmtree(home, ignore_errors=True)
        raise Refused(2, f"cannot start {visible(cmd[0])}: {error.strerror or error}")
    pgid = proc.pid

    if args.serve:
        started = _ps_start(pgid)

        def change(data):
            e = data.setdefault(args.repo, {}).setdefault(str(args.number), {})
            e.setdefault("pgids", []).append(pgid)
            if started:
                e.setdefault("pgid_started", {})[str(pgid)] = started
            e.setdefault("executed", []).append(dict(record, serve=True, pgid=pgid, port=args.port))

        _update(change)
        emit({"pgid": pgid, "port": args.port, "log": log})
        return 0

    timed_out = False
    try:
        code = proc.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group(pgid)
        code = proc.wait()
    kill_group(pgid)  # anything the command left running in the background
    shutil.rmtree(home, ignore_errors=True)
    record["exit"] = None if timed_out else code

    def change(data):
        e = data.setdefault(args.repo, {}).setdefault(str(args.number), {})
        e.setdefault("executed", []).append(dict(record, timed_out=timed_out))

    _update(change)
    emit({"exit": record["exit"], "timed_out": timed_out, "log": log, "tail": tail_of(log)})
    return 0


def emit(data):
    print(json.dumps(data, indent=1, sort_keys=True))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="review_worktree.py", description="A PR head's review worktree, and approved runs in it.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    sub = parser.add_subparsers(dest="action", required=True)

    def common(sp, need_dir=True):
        sp.add_argument("-R", "--repo", required=True, help="OWNER/REPO")
        sp.add_argument("-n", "--number", type=int, required=True, help="pull request number")
        if need_dir:
            sp.add_argument("-C", dest="C", required=True, help="local clone whose origin is OWNER/REPO")
            sp.add_argument("--commit", required=True, help="pinned head SHA")

    common(sub.add_parser("add", help="check out the PR head detached"))
    rm = sub.add_parser("remove", help="kill tracked servers, remove the worktree, drop state")
    common(rm, need_dir=False)
    rm.add_argument("--all-heads", action="store_true", help="also sweep unrecorded heads of this PR")
    ls = sub.add_parser("list", help="recorded worktrees")
    ls.add_argument("-R", "--repo", help="OWNER/REPO")
    run = sub.add_parser("run", help="front end only: run a command the user approved")
    common(run)
    run.add_argument("--approved", required=True, help="the user's own words approving this run")
    run.add_argument("--timeout", type=int, default=600, help="seconds before a foreground run is killed")
    run.add_argument("--serve", action="store_true", help="start in the background; return its pgid")
    run.add_argument("--port", type=int, help="PORT for --serve (localhost)")
    run.add_argument("cmd", nargs=argparse.REMAINDER, help="-- CMD ...")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after the first "--" is the command, whatever it looks like;
    # argparse's own handling of "--" before REMAINDER differs across versions.
    cmd = None
    if "--" in argv:
        cut = argv.index("--")
        argv, cmd = argv[:cut], argv[cut + 1:]
    args = build_parser().parse_args(argv)
    if cmd is not None:
        args.cmd = cmd
    handler = {"add": cmd_add, "remove": cmd_remove, "list": cmd_list, "run": cmd_run}[args.action]
    try:
        return handler(args)
    except Refused as refusal:
        print(f"review_worktree: {refusal}", file=sys.stderr)
        return refusal.code


if __name__ == "__main__":
    sys.exit(main())
