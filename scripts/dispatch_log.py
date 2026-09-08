#!/usr/bin/env python3
"""dispatch_log: the append-only record of every subagent dispatch the guard saw.

WHY A LOG AT ALL. dispatch_guard.py decides one call at a time and can never see
a fan-out, so the signals that need a second dispatch to interpret -- was a block
followed by a compliant re-dispatch, was a small brief one of five siblings --
are recorded here and resolved at read time. Judgment that lives in the reader
costs nothing on the hot path and can be revised without a Codex /hooks
re-approval.

NEVER PROMPT TEXT. This file sits in a home directory forever and would otherwise
accumulate briefs about whatever Leo works on. Prompts and working directories
are stored as truncated SHA-256, which is enough to notice the same brief
re-dispatched after a block. Hashes reduce exposure but guessable text can still
be identified by hashing candidate values. Raw text only
under LEOS_AGENT_DISPATCH_LOG_PROMPTS=1, truncated, and documented as debug-only.

The file is ${LEOS_AGENT_LOCAL_PATH:-$HOME/.leos-agent-local}/dispatch.jsonl,
beside routing.json and the handoffs -- data lives with the data, never inside
the plugin, so an upgrade or an uninstall cannot take it.

  dispatch_log.py report [--limit N] [--json]   what the guard has been seeing
  dispatch_log.py path                          the log's path

Exit codes: 0 ok, 2 on bad usage.
"""
import argparse
import collections
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Same data root and the same lock discipline as every other machine-local file.
# _locked takes any path (it locks a sibling .lock), which is why a .jsonl can
# use it even though state_file() would force a .json suffix.
from state import _data_root, _locked  # noqa: E402

LOG_NAME = "dispatch.jsonl"

# One megabyte, one generation. ~230 bytes per record is roughly 4,500 dispatches
# per file and 9,000 retained -- months of history, bounded at 2 MiB forever, with
# no cron job and nothing to configure.
MAX_BYTES = 1 << 20

RECORD_VERSION = 2


TIER_PREFIX = "leo-"


def is_tier(agent):
    """Is this one of the economical-tier agents?

    Match the bare name after any namespace. A plugin install namespaces the
    type -- Claude Code dispatches `leos-agent:leo-runner`, not `leo-runner` --
    and a raw prefix test rejects that, which blocked the exact path the refusal
    message recommends. That is the worst false positive this guard can have, so
    the one place that decides it is shared rather than repeated.
    """
    from routing_engine import tier_for
    return tier_for(agent) is not None


def path():
    return os.path.join(_data_root(), LOG_NAME)


def digest(text):
    """A short hash for correlation; not encryption or an anonymity guarantee."""
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _keep_prompts():
    return os.environ.get("LEOS_AGENT_DISPATCH_LOG_PROMPTS") == "1"


def record(dispatch, decision, reason, harness, session=None, cwd=None, trivial=0):
    """The on-disk shape for one dispatch. Pure; writes nothing."""
    entry = {
        "v": RECORD_VERSION,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "harness": harness,
        "session": digest(session),
        # Parallel dispatches from one assistant message land in the same
        # two-second bucket, which is how `report` tells a fan-out from a
        # sequence of lone spawns without the hot path ever reading the log.
        "burst": "%s:%d" % (digest(session) or "-", int(time.time() // 2)),
        "project": digest(cwd),
        "decision": decision,
        "reason": reason,
    }
    if dispatch is not None:
        entry.update({
            "tool": dispatch.tool,
            "agent": dispatch.agent,
            "model": dispatch.model,
            "trivial": trivial,
            "prompt_bytes": dispatch.prompt_bytes,
            "prompt_lines": dispatch.prompt_lines,
            "paths": dispatch.path_count,
            "prompt": dispatch.prompt_hash,
        })
        if _keep_prompts():
            entry["prompt_text"] = dispatch.prompt_head
    return entry


def append(entry):
    """Append one record. Rotates at MAX_BYTES. Raises only on real I/O trouble.

    Callers must treat a failure here as cosmetic: the guard's decision is made
    before this is called and emitted after it, so a read-only home or a full
    disk costs a log line, never a dispatch.
    """
    target = path()
    with _locked(target):
        try:
            if os.path.getsize(target) > MAX_BYTES:
                os.replace(target, target + ".1")
        except OSError:
            pass  # absent, or unstattable; either way there is nothing to rotate
        # 0600 explicitly: open("a") yields 0644 under a default umask, which is
        # the wrong mode for a file that indexes Leo's projects even in hashes.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")


def read(limit=None, target=None):
    """Records, oldest first. Tolerates a truncated final line from a crash."""
    out = []
    for candidate in ((target,) if target else (path() + ".1", path())):
        try:
            with open(candidate, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue  # a half-written last line, or a foreign line
                    if isinstance(entry, dict):
                        out.append(entry)
        except FileNotFoundError:
            continue
        except OSError as exc:
            sys.exit("dispatch_log: %s: %s" % (candidate, exc.strerror or exc))
    return out[-limit:] if limit else out


def summarise(entries):
    """The read-time judgment: burst collapse, tiers, and block conversion."""
    dispatches = [e for e in entries if e.get("decision") not in ("executed", "completed")]
    # Allowed attempts count toward a burst; execution is not established. A block and the
    # re-dispatch it forced land in the same two-second bucket, and counting
    # both would let every blocked retry pose as a fan-out of two.
    bursts = collections.Counter(
        e.get("burst") for e in dispatches if e.get("burst") and e.get("decision") not in ("block", "error")
    )

    tiers = collections.Counter()
    for entry in dispatches:
        agent = entry.get("agent") or ""
        if is_tier(agent):
            tiers[agent] += 1
        elif entry.get("model"):
            tiers["explicit model"] += 1
        elif entry.get("decision") == "block":
            tiers["blocked"] += 1
        else:
            tiers["model unspecified"] += 1

    # A trivial-looking brief that was one of several in the same burst is a
    # fan-out, which the policy wants. Only a lone small spawn is a finding --
    # and only one that actually ran: a blocked dispatch spent nothing, so it
    # cannot also be an over-delegation.
    trivial = [
        e for e in dispatches
        if e.get("trivial", 0) >= 2
        and e.get("decision") not in ("block", "error")
        and bursts.get(e.get("burst"), 0) < 2
    ]

    # A prompt hash is evidence of similar text, not of successful execution,
    # lower spend, or a causal retry. Count blocks even when no hash is available.
    blocked = [e for e in entries if e.get("decision") == "block"]
    confirmed = [e for e in entries if e.get("decision") == "executed"]

    return {
        "records": len(entries),
        "dispatch_attempts": len([e for e in dispatches if e.get("decision") != "error"]),
        "window": [entries[0].get("ts"), entries[-1].get("ts")] if entries else [],
        "harnesses": dict(collections.Counter(e.get("harness") for e in entries)),
        "decisions": dict(collections.Counter(e.get("decision") for e in entries)),
        "tiers": dict(tiers),
        "errors": sum(1 for e in entries if e.get("decision") == "error"),
        "blocked": len(blocked),
        "confirmed_executions": len(confirmed),
        "converted": None,  # legacy field: never infer savings from prompt hashes
        "trivial_lone_spawns": len(trivial),
        "agents": dict(collections.Counter(
            "%s @ %s" % (e.get("agent") or "-", e.get("effective_model") or e.get("requested_model") or e.get("model") or "unknown")
            for e in entries
        )),
    }


def render(summary):
    lines = []
    # Errors lead. Conflating "the guard crashed" with "the guard allowed" is how
    # a dead guard goes unnoticed for months, so a nonzero count is the headline.
    if summary["errors"]:
        lines.append("!! %d guard error(s) -- the guard failed open this many times" % summary["errors"])
    if not summary["records"]:
        lines.append("no dispatches recorded yet (%s)" % path())
        return "\n".join(lines)

    lines.append("%d lifecycle record(s)  %s .. %s" % (summary["records"], summary["window"][0], summary["window"][1]))
    lines.append("  dispatch attempts  %d; child model observations  %d" % (summary["dispatch_attempts"], summary["confirmed_executions"]))
    lines.append("  harnesses   " + ", ".join("%s %d" % kv for kv in sorted(summary["harnesses"].items())))
    lines.append("  tiers       " + ", ".join("%s %d" % kv for kv in sorted(summary["tiers"].items())))
    if summary["blocked"]:
        lines.append("  blocks      %d (execution/savings not inferred from retries)" % summary["blocked"])
    lines.append("  short-brief signals  %d  (heuristic only; not evidence of wasted spend)" % summary["trivial_lone_spawns"])
    lines.append("  agent @ model:")
    for name, count in sorted(summary["agents"].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append("    %-44s %d" % (name, count))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dispatch_log.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="what the guard has been seeing")
    report.add_argument("--limit", type=int, default=None, help="only the most recent N records")
    report.add_argument("--json", action="store_true", help="machine-readable summary")
    sub.add_parser("path", help="the log file's path")

    args = parser.parse_args(argv)
    if args.command == "path":
        print(path())
        return 0

    summary = summarise(read(limit=args.limit))
    print(json.dumps(summary, indent=1, sort_keys=True) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
