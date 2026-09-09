#!/usr/bin/env python3
"""Tag the tip of main so the release workflow publishes it, or defer if main moved.

Every commit on main becomes a release, so the only thing two runs can collide
over is the pair of refs a release is made of: the commit carrying the new
version and the tag naming it. Preparing one therefore reduces to a single
compare-and-swap -- bump, verify, commit, tag, then push the branch and the tag
in one atomic transaction. The remote accepts that push only if main is still
exactly where this run found it, so a run that loses the race has changed
nothing at all, locally or remotely.

A loser defers instead of retrying, and that is the important decision here.
The push that beat it starts its own workflow run, and that run releases the
newer tip -- which already contains the commit this run was triggered for.
Retrying would instead re-derive a version against a tree this run never
verified, and two runs retrying against each other is how one commit ends up
with two versions on the registry. For the same reason this never resets onto a
newer tip: a run releases exactly the commit it was triggered for, or nothing.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_bump(root=ROOT):
	"""Import bump.py rather than shelling out to it.

	Imported for two reasons. It derives the version from the current UTC date,
	so a preview call and an apply call can straddle midnight and answer with
	two different versions for one release; calling next_version once removes
	that entirely. And it is the only place that knows which files carry a
	version, so a second copy of that list here would be free to drift out of
	step with it -- and a release commit that misses a file leaves the tag it
	pushes carrying a tree no manifest check can pass.
	"""
	spec = importlib.util.spec_from_file_location("leos_agent_bump", root / "scripts" / "bump.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


BUMP = load_bump()

# The files bump.py owns. A release commit stages these and nothing else, so
# whatever else a runner's checkout may be carrying cannot ride along into a tag.
VERSIONED = tuple(BUMP.REWRITE)

# Push rejections that mean another push won a ref this run was about to move.
# Matched as an allowlist on purpose: a declined branch hook, a bad credential,
# or a dropped connection is also a rejection, and calling any of those a race
# would turn a broken release pipeline into a silent one.
RACE_REJECTIONS = ("fetch first", "non-fast-forward", "stale info")
TAG_TAKEN_REJECTION = "already exists"

DEFAULT_AUTHOR_NAME = "github-actions[bot]"
DEFAULT_AUTHOR_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class TagError(Exception):
	"""The release cannot be prepared safely; the caller should stop, not retry."""


def git(*args, root=ROOT, check=True):
	result = subprocess.run(
		["git", *args], cwd=str(root), capture_output=True, text=True, check=False
	)
	if check and result.returncode:
		raise TagError(f"git {' '.join(args)} failed: {(result.stdout + result.stderr).strip()}")
	return result


def head_sha(root=ROOT):
	return git("rev-parse", "HEAD", root=root).stdout.strip()


def dirty_paths(root=ROOT):
	"""Paths git reports as modified, staged, or untracked (ignored files excluded)."""
	lines = git("status", "--porcelain", root=root).stdout.splitlines()
	return sorted(line[3:].strip() for line in lines if line.strip())


def apply_bump(root=ROOT):
	"""Choose the next version once and write it across every versioned file.

	rewrite_all validates every rewrite before it replaces anything and restores
	what it wrote if one fails, so this either leaves the tree fully bumped or
	exactly as it found it.
	"""
	try:
		current = BUMP.current_version(root)
		version = BUMP.next_version(current)
		if version == current:
			raise TagError(f"bump.py chose {version}, which is already released")
		BUMP.rewrite_all(root, current, version)
	except BUMP.BumpError as exc:
		raise TagError(f"the version could not be bumped: {exc}") from exc
	return version


def verify(commands, root=ROOT):
	"""Run each check against the bumped tree, before anything is committed.

	This is what keeps a broken main from producing a tag nothing can ever
	publish: the tag and the release commit only exist once every check the
	publish step will repeat has already passed on this exact tree.

	Run through a shell, so a workflow can pass the same command lines it would
	put in a `run:` step -- globs included -- rather than an argv the caller has
	to keep pre-expanded. The commands are literals from the workflow file.
	"""
	for command in commands:
		result = subprocess.run(command, shell=True, cwd=str(root), check=False)
		if result.returncode:
			raise TagError(f"verification failed ({command}); nothing was committed or pushed")


def commit_release(version, sha, root=ROOT, name=DEFAULT_AUTHOR_NAME, email=DEFAULT_AUTHOR_EMAIL):
	message = f"Release {version}\n\nTagged automatically by the release workflow for {sha[:7]}.\n"
	git("add", "--", *VERSIONED, root=root)
	git(
		"-c", f"user.name={name}", "-c", f"user.email={email}",
		"commit", "--message", message, "--", *VERSIONED,
		root=root,
	)
	return head_sha(root)


def create_tag(tag, root=ROOT, name=DEFAULT_AUTHOR_NAME, email=DEFAULT_AUTHOR_EMAIL):
	# Annotated with the tag name as its message, matching every release tag
	# already on the remote. Unsigned, because CI holds no signing key.
	git(
		"-c", f"user.name={name}", "-c", f"user.email={email}",
		"tag", "--annotate", "--message", tag, tag,
		root=root,
	)


def atomic_push(remote, branch, tag, root=ROOT):
	"""Move the branch and the tag together, or move neither.

	--atomic is what makes the outcome binary. Without it a rejected branch
	update can still leave the tag published, and a tag whose commit is not on
	main fails the release workflow's own ancestor check -- a stuck release that
	has to be cleaned up by hand.
	"""
	result = git(
		"push", "--atomic", remote, f"HEAD:refs/heads/{branch}", f"refs/tags/{tag}",
		root=root, check=False,
	)
	return result.returncode == 0, (result.stdout + result.stderr).strip()


def classify_rejection(output):
	lowered = output.lower()
	if any(marker in lowered for marker in RACE_REJECTIONS):
		return "raced"
	if TAG_TAKEN_REJECTION in lowered:
		return "tag-taken"
	return "unknown"


def push_landed(remote, branch, commit, root=ROOT):
	"""Did the push apply despite reporting failure?

	Only reached when a push fails for a reason that is not a rejection -- a
	connection dropped after the remote committed the transaction, most
	plausibly. Checking the branch alone is enough: the push was atomic, so the
	tag moved if and only if the branch did.
	"""
	result = git("ls-remote", remote, f"refs/heads/{branch}", root=root, check=False)
	if result.returncode:
		return False
	fields = result.stdout.split()
	return bool(fields) and fields[0] == commit


def undo_local(tag, sha, root=ROOT):
	"""Put the checkout back where this run found it.

	A losing run has to leave nothing behind on either side. The remote is
	already untouched -- the push was rejected whole -- and this is the local
	half, which matters for anyone running the script outside a runner that
	throws its checkout away.
	"""
	git("tag", "--delete", tag, root=root, check=False)
	git("reset", "--hard", "--quiet", sha, root=root, check=False)


def emit_outputs(path, tag):
	if not path:
		return
	with open(path, "a", encoding="utf-8") as handle:
		handle.write(f"tag={tag}\n")
		handle.write(f"released={'true' if tag else 'false'}\n")


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--remote", default="origin", help="remote holding the release branch")
	parser.add_argument("--branch", default="main", help="branch this release is cut from")
	parser.add_argument("--expect-sha", help="commit this run was triggered for; HEAD must match it")
	parser.add_argument("--verify", action="append", default=[], metavar="COMMAND",
	                    help="run against the bumped tree before committing; repeatable")
	parser.add_argument("--author-name", default=DEFAULT_AUTHOR_NAME)
	parser.add_argument("--author-email", default=DEFAULT_AUTHOR_EMAIL)
	parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"),
	                    help="file to append step outputs to")
	parser.add_argument("--dry-run", action="store_true", help="bump and verify; commit, tag, and push nothing")
	args = parser.parse_args(argv)

	try:
		if args.expect_sha:
			found = head_sha()
			if found != args.expect_sha:
				raise TagError(
					f"HEAD is {found}, not the {args.expect_sha} this run was triggered for; "
					"refusing to release a commit nobody asked for"
				)

		dirty = dirty_paths()
		if dirty:
			raise TagError("refusing to release a dirty checkout: " + ", ".join(dirty))

		version = apply_bump()
		tag = f"v{version}"
		if git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0:
			raise TagError(
				f"{tag} already exists in this checkout; {args.branch}'s version is behind the "
				"release tags, so bump.py cannot derive an unused one"
			)

		verify(args.verify)

		if args.dry_run:
			print(f"would release {tag} from {head_sha()}", flush=True)
			# The bump had to happen for the verification above to mean anything,
			# so put the tree back rather than leaving a rehearsal on disk.
			git("checkout", "--", *VERSIONED)
			return 0

		released_sha = head_sha()
		commit = commit_release(version, released_sha, name=args.author_name, email=args.author_email)
		create_tag(tag, name=args.author_name, email=args.author_email)

		pushed, output = atomic_push(args.remote, args.branch, tag)
		if pushed:
			emit_outputs(args.github_output, tag)
			print(f"released {tag} at {commit}", flush=True)
			return 0

		verdict = classify_rejection(output)
		if verdict == "raced":
			# Nothing moved: the push was atomic, so the tag was rejected with the
			# branch. The commit that beat this one has its own run queued, and it
			# releases a tip that already contains everything this run was carrying.
			undo_local(tag, released_sha)
			emit_outputs(args.github_output, "")
			print(
				f"{args.branch} moved while this run prepared {tag}; deferring to the run "
				f"for the newer tip. Nothing was pushed.\n{output}",
				flush=True,
			)
			return 0
		if verdict == "tag-taken":
			raise TagError(
				f"{tag} already exists on {args.remote} but {args.branch} did not move; "
				f"{args.branch}'s version and the release tags disagree, and no bump can "
				f"resolve that from here: {output}"
			)
		if push_landed(args.remote, args.branch, commit):
			emit_outputs(args.github_output, tag)
			print(f"released {tag} at {commit}", flush=True)
			print(f"warning: the push reported failure but the remote is at {commit}: {output}",
			      file=sys.stderr)
			return 0
		raise TagError(f"git push failed for a reason that is not a race: {output}")
	except TagError as exc:
		print(f"error: {exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
