#!/usr/bin/env python3
"""Resolve an identifier (PR number/URL, branch name, or ticket id) to everything
`/attach-pr` needs: the branch, its PR, and the working directory to attach from.

Prints a single JSON object to stdout:

  {"status": "ok", "branch": ..., "pr_number": ..., "pr_url": ..., "base_ref": ...,
   "pr_state": ..., "pr_title": ..., "workdir": ... | null, "workdir_kind": ...,
   "attach_command": ...}
  {"status": "ambiguous", "message": ..., "candidates": [ {...}, ... ]}
  {"status": "error", "message": ...}

Exit code is 0 for "ok" and 1 otherwise, so callers can branch on it directly.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from hashlib import sha256

PR_NUM_RE = re.compile(r"^#?(\d+)$")
PR_URL_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9]\d*)$")
TICKET_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-(\d+)$")
SAFE_REF_RE = re.compile(r"^(?!-)[A-Za-z0-9][A-Za-z0-9._/-]*$")

PR_FIELDS = "number,url,headRefName,baseRefName,state,title"


def run(cmd, cwd=None):
    """Run a command, returning (returncode, stdout, stderr) with output stripped."""
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def die(message):
    print(json.dumps({"status": "error", "message": message}, indent=2))
    sys.exit(1)


def emit(payload):
    print(json.dumps(payload, indent=2))
    sys.exit(0 if payload.get("status") == "ok" else 1)


def is_safe_pr_url(url):
    """Accept only a canonical public GitHub pull-request URL."""
    return isinstance(url, str) and bool(PR_URL_RE.fullmatch(url))


def is_safe_ref(ref):
    """Keep refs shell-safe *and* ask Git to enforce its ref grammar."""
    if not isinstance(ref, str) or not SAFE_REF_RE.fullmatch(ref):
        return False
    rc, _, _ = run(["git", "check-ref-format", "--branch", ref])
    return rc == 0


def suggested_worktree(root, branch):
    """A readable name with a stable digest prevents slash-to-dash collisions."""
    readable = branch.replace("/", "-")
    digest = sha256(branch.encode("utf-8")).hexdigest()[:10]
    return os.path.join(root, ".claude", "worktrees", f"{readable}-{digest}")


def build_attach_command(workdir, pr_url, base_ref, branch):
    """Build the intentional compound attach command with every value quoted."""
    return (
        'gh() { echo "$PR_URL"; }; '
        f"cd {shlex.quote(workdir)}; "
        f"PR_URL={shlex.quote(pr_url)} gh pr create --draft "
        f"--base {shlex.quote(base_ref)} --head {shlex.quote(branch)}"
    )


# --- environment checks ----------------------------------------------------


def repo_root():
    rc, out, _ = run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0:
        die("not inside a git repository — cd into the repo before running /attach-pr")
    return out


def require_gh():
    if not shutil.which("gh"):
        die("`gh` is not installed or not on PATH; /attach-pr needs the GitHub CLI")
    rc, _, err = run(["gh", "auth", "status"])
    if rc != 0:
        die(f"`gh` is not authenticated: {err or 'run `gh auth login`'}")


def name_with_owner():
    rc, out, err = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if rc != 0:
        die(f"could not determine the GitHub repo for this directory: {err}")
    return out


# --- git helpers -----------------------------------------------------------


def worktrees():
    """Map branch name -> worktree path, from `git worktree list --porcelain`."""
    rc, out, _ = run(["git", "worktree", "list", "--porcelain"])
    if rc != 0:
        return {}
    result, path = {}, None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch ") and path:
            branch = line[len("branch ") :]
            if branch.startswith("refs/heads/"):
                result[branch[len("refs/heads/") :]] = path
    return result


def branch_exists_local(branch):
    rc, _, _ = run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    return rc == 0


def branch_exists_remote(branch):
    rc, out, _ = run(["git", "ls-remote", "--heads", "origin", branch])
    return rc == 0 and bool(out)


def all_known_branches():
    rc, out, _ = run(
        [
            "git",
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
            "refs/remotes/origin",
        ]
    )
    if rc != 0:
        return []
    names = []
    for ref in out.splitlines():
        name = ref[len("origin/") :] if ref.startswith("origin/") else ref
        if name and name != "HEAD" and name not in names:
            names.append(name)
    return names


# --- PR lookups ------------------------------------------------------------


def pr_by_number(number):
    rc, out, err = run(["gh", "pr", "view", str(number), "--json", PR_FIELDS])
    if rc != 0:
        if "Could not resolve to a PullRequest" in err:
            return None, f"there is no pull request #{number} in this repo"
        return None, err or f"no pull request #{number} in this repo"
    try:
        return json.loads(out), None
    except json.JSONDecodeError as exc:
        return None, f"could not parse `gh pr view` output: {exc}"


def prs_for_branch(branch):
    rc, out, err = run(
        [
            "gh", "pr", "list", "--head", branch, "--state", "all",
            "--json", PR_FIELDS, "--limit", "20",
        ]
    )
    if rc != 0:
        return [], err or f"`gh pr list` failed for branch {branch}"
    try:
        return json.loads(out), None
    except json.JSONDecodeError as exc:
        return [], f"could not parse `gh pr list` output: {exc}"


def prs_by_search(term):
    rc, out, _ = run(
        [
            "gh", "pr", "list", "--search", term, "--state", "all",
            "--json", PR_FIELDS, "--limit", "20",
        ]
    )
    if rc != 0:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def pick_one(prs):
    """Prefer the single OPEN PR; otherwise the highest-numbered one."""
    open_prs = [p for p in prs if p.get("state") == "OPEN"]
    if len(open_prs) == 1:
        return open_prs[0]
    pool = open_prs or prs
    return max(pool, key=lambda p: p.get("number", 0)) if pool else None


def as_candidate(pr):
    return {
        "pr_number": pr.get("number"),
        "url": pr.get("url"),
        "branch": pr.get("headRefName"),
        "base_ref": pr.get("baseRefName"),
        "state": pr.get("state"),
        "title": pr.get("title"),
    }


# --- resolution ------------------------------------------------------------


def resolve(identifier, repo):
    """Return (pr_dict, note) or emit an error/ambiguous payload and exit."""
    ident = identifier.strip()

    url_match = PR_URL_RE.fullmatch(ident)
    if url_match:
        owner, name, number = url_match.groups()
        if f"{owner}/{name}".lower() != repo.lower():
            die(
                f"that PR URL belongs to {owner}/{name}, but this directory is {repo}. "
                "cd into the right repo, or pass an identifier from this one."
            )
        pr, err = pr_by_number(number)
        if not pr:
            die(err)
        return pr, f"resolved from PR URL #{number}"

    num_match = PR_NUM_RE.match(ident)
    if num_match:
        number = num_match.group(1)
        pr, err = pr_by_number(number)
        if not pr:
            die(err)
        return pr, f"resolved from PR number #{number}"

    # A ticket-shaped identifier may also be a literal branch name — colony's bare-ticket
    # branches (`DOCS-5943`) and kebab variants (`docs-6171`) both look like ticket ids.
    # An exact branch match is the more specific reading, so it wins; ticket search is the
    # fallback for ids that name no branch directly.
    if TICKET_RE.match(ident):
        if branch_exists_local(ident) or branch_exists_remote(ident):
            return resolve_branch(ident), f"resolved from branch {ident} (ticket-shaped name)"
        return resolve_ticket(ident), f"resolved from ticket {ident.upper()}"

    return resolve_branch(ident), f"resolved from branch {ident}"


def resolve_branch(branch):
    local = branch_exists_local(branch)
    remote = branch_exists_remote(branch)
    if not local and not remote:
        die(
            f"branch `{branch}` does not exist locally or on origin. "
            "Check the name (`git branch -a`), or pass a PR number or ticket id instead."
        )

    prs, err = prs_for_branch(branch)
    if err:
        die(err)
    if not prs:
        where = "locally and on origin" if local and remote else ("locally" if local else "on origin")
        die(
            f"branch `{branch}` exists {where} but has no pull request "
            "(any state). Open a PR for it first — /attach-pr only attaches to an existing PR."
        )
    if len(prs) > 1:
        chosen = pick_one(prs)
        if not (chosen and chosen.get("state") == "OPEN" and
                sum(1 for p in prs if p.get("state") == "OPEN") == 1):
            emit({
                "status": "ambiguous",
                "message": f"branch `{branch}` has {len(prs)} pull requests; ask which one to attach.",
                "candidates": [as_candidate(p) for p in prs],
            })
        return chosen
    return prs[0]


def resolve_ticket(ticket):
    """Ticket-tracker-agnostic: match the id against branch names and PR text."""
    key = ticket.upper()
    found = {}

    for branch in all_known_branches():
        if key in branch.upper():
            prs, _ = prs_for_branch(branch)
            for pr in prs:
                found[pr["number"]] = pr

    for pr in prs_by_search(key):
        haystack = f"{pr.get('title', '')} {pr.get('headRefName', '')}".upper()
        if key in haystack:
            found.setdefault(pr["number"], pr)

    prs = list(found.values())
    if not prs:
        die(
            f"found no branch or pull request referencing `{key}` in this repo. "
            "If the ticket exists but no PR does yet, there is nothing to attach; "
            "otherwise pass the branch name or PR number directly."
        )
    if len(prs) > 1:
        open_prs = [p for p in prs if p.get("state") == "OPEN"]
        if len(open_prs) != 1:
            emit({
                "status": "ambiguous",
                "message": f"`{key}` matches {len(prs)} pull requests; ask which one to attach.",
                "candidates": [as_candidate(p) for p in prs],
            })
        return open_prs[0]
    return prs[0]


# --- working directory -----------------------------------------------------


def resolve_workdir(branch, root):
    """Where to attach from: an existing worktree, the base checkout, or nowhere."""
    wts = worktrees()
    if branch in wts:
        path = wts[branch]
        kind = "worktree" if os.path.realpath(path) != os.path.realpath(root) else "checkout"
        return path, kind

    rc, current, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if rc == 0 and current == branch:
        return root, "checkout"

    return None, "not_checked_out"


def main():
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        die("usage: resolve_attach_target.py <pr-number|pr-url|branch|TICKET-123>")

    root = repo_root()
    require_gh()
    repo = name_with_owner()

    pr, note = resolve(sys.argv[1], repo)
    branch = pr.get("headRefName")
    base_ref = pr.get("baseRefName") or "main"
    pr_url = pr.get("url")
    if not branch:
        die(f"PR #{pr.get('number')} has no head branch recorded; cannot attach")
    if not is_safe_ref(branch) or not is_safe_ref(base_ref):
        die("PR branch or base ref contains unsupported shell-unsafe characters")
    if not is_safe_pr_url(pr_url):
        die("PR URL is not a canonical https://github.com/<owner>/<repo>/pull/<number> URL")

    workdir, kind = resolve_workdir(branch, root)

    payload = {
        "status": "ok",
        "note": note,
        "repo": repo,
        "branch": branch,
        "pr_number": pr.get("number"),
        "pr_url": pr_url,
        "base_ref": base_ref,
        "pr_state": pr.get("state"),
        "pr_title": pr.get("title"),
        "workdir": workdir,
        "workdir_kind": kind,
        "repo_root": root,
        "suggested_worktree": suggested_worktree(root, branch),
    }
    if workdir:
        payload["attach_command"] = build_attach_command(workdir, pr_url, base_ref, branch)
    emit(payload)


if __name__ == "__main__":
    main()
