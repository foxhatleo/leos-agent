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

# One megabyte, one generation. A v3 dispatch record carries the full price
# comparison plus tier and escalation fields, roughly 850 bytes; a completion
# record with outcome and usage is about 350. Call it 1,100 dispatches per file
# and 2,200 retained. Still months of history for one person, still bounded at
# 2 MiB forever, with nothing to configure -- but worth knowing before reading a
# report and assuming it covers the whole period.
MAX_BYTES = 1 << 20

# v3 added tier, escalation_from, outcome, verified, outcome_source, usage.
# The reader branches on this so "predates instrumentation" is never confused
# with "the worker emitted no Result line".
RECORD_VERSION = 3


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
    from routing_engine import tier_for
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
            "tier": tier_for(dispatch.agent),
            "escalation_from": dispatch.escalation_from,
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
            # Raise, don't exit: read() serves hooks and scanners that must file
            # an unreadable log as a finding and carry on. A SystemExit here
            # skipped a Codex lifecycle hook's mandatory JSON reply and took the
            # diagnosis bundle down before it wrote anything. The CLI exits in main().
            raise OSError("%s: %s" % (candidate, exc.strerror or exc)) from exc
    return out[-limit:] if limit else out


COMPLETION = ("executed", "completed")


def _tier_of(row):
    """A dispatch or completion row's tier: v3 records it; older rows recompute."""
    from routing_engine import tier_for
    return row.get("tier") or tier_for(row.get("agent") or "") or None


def _ts(row):
    return row.get("ts") or ""


def join(entries):
    """Pair each completion row with the dispatch it finished. Read time only.

    Order of evidence, each dispatch claimed at most once: call_id; (session,
    agent_id); nearest preceding same-session same-tier dispatch that actually
    ran; else unmatched. A tie or a second candidate at the same strength is
    `ambiguous`, and an ambiguous outcome is never credited to a real tier.
    Hermes may report one delegation twice (post_tool_call and subagent_stop);
    those collapse here, preferring the row that carries an outcome.
    """
    dispatches = [e for e in entries if e.get("decision") not in COMPLETION and e.get("decision") not in ("block", "error")
                  and (e.get("agent") or e.get("tool"))]
    completions = []
    seen = {}
    for row in sorted((e for e in entries if e.get("decision") in COMPLETION), key=_ts):
        key = (row.get("harness"), row.get("session"), row.get("call_id")) if row.get("call_id") else None
        if key is None and row.get("agent_id"):
            key = (row.get("harness"), row.get("session"), "agent", row.get("agent_id"))
        if key is None:
            key = (row.get("harness"), row.get("session"), row.get("agent"), _ts(row)[:18])
        prior = seen.get(key)
        if prior is None:
            seen[key] = row
            completions.append(row)
        elif (prior.get("outcome") in (None, "unknown")) and row.get("outcome") not in (None, "unknown"):
            completions[completions.index(prior)] = row
            seen[key] = row
        elif prior.get("decision") == "completed" and row.get("decision") == "executed":
            completions[completions.index(prior)] = row
            seen[key] = row
    by_call = collections.defaultdict(list)
    for d in dispatches:
        if d.get("call_id"):
            by_call[(d.get("harness"), d.get("session"), d["call_id"])].append(d)
    claimed = set()
    pairs = {}
    stats = collections.Counter()
    for row in completions:
        tier = None
        candidates = [d for d in by_call.get((row.get("harness"), row.get("session"), row.get("call_id")), []) if id(d) not in claimed] if row.get("call_id") else []
        how = "call_id" if candidates else None
        if not candidates and row.get("session") and row.get("agent_id"):
            # Dispatch rows never know the child's agent_id, so this key only
            # ever matches a dispatch that a later adapter learns to stamp.
            candidates = [d for d in dispatches if id(d) not in claimed and d.get("session") == row.get("session")
                          and d.get("agent_id") == row.get("agent_id")]
            how = "agent_id" if candidates else None
        if not candidates:
            want = _tier_of(row)
            preceding = [d for d in dispatches if id(d) not in claimed and d.get("harness") == row.get("harness")
                         and d.get("session") == row.get("session") and _ts(d) <= _ts(row)
                         and (want is None or _tier_of(d) == want)]
            if preceding:
                latest = max(_ts(d) for d in preceding)
                candidates = [d for d in preceding if _ts(d) == latest]
                how = "nearest"
        if not candidates:
            stats["unmatched"] += 1
            pairs[id(row)] = (None, "unrouted" if _tier_of(row) is None else _tier_of(row), "unmatched")
            continue
        chosen = candidates[-1]
        claimed.add(id(chosen))
        if len(candidates) > 1:
            stats["ambiguous"] += 1
            tier = "ambiguous"
        else:
            stats[how] += 1
            tier = _tier_of(chosen) or "unrouted"
        pairs[id(row)] = (chosen, tier, how)
    return completions, pairs, dict(stats)


def summarise(entries):
    """The read-time judgment: burst collapse, tiers, and block conversion."""
    # A child whose model arrived late has two rows: the SubagentStop
    # "completed" and the SessionEnd "executed" that supersedes it. Count each
    # child once, in its final state, so decisions and agents describe agents
    # rather than rows. records stays the raw retained count.
    executed_children = {
        (e.get("session"), e.get("agent_id")) for e in entries
        if e.get("decision") == "executed" and e.get("session") and e.get("agent_id")
    }
    superseded = {
        id(e) for e in entries
        if e.get("decision") == "completed" and (e.get("session"), e.get("agent_id")) in executed_children
    }
    current = [e for e in entries if id(e) not in superseded]
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

    completions, pairs, joins = join(entries)
    outcomes = {}
    verified = {}
    usage = {}
    usage_rows = collections.Counter()
    sources = collections.defaultdict(collections.Counter)
    for row in completions:
        _, tier, _how = pairs[id(row)]
        if "outcome" in row:
            outcomes.setdefault(tier, collections.Counter())[row.get("outcome") or "unknown"] += 1
            state = {True: "stated", False: "none"}.get(row.get("verified"), "unstated")
            verified.setdefault(tier, collections.Counter())[state] += 1
            sources[row.get("harness")][row.get("outcome_source") or "none"] += 1
        if isinstance(row.get("usage"), dict):
            import outcome as _outcome
            usage[tier] = _outcome.add_usage(usage.get(tier), row["usage"])
            usage_rows[tier] += 1
    escalations = collections.Counter()
    unobservable = 0
    for entry in dispatches:
        source = entry.get("escalation_from")
        if source == "unobservable":
            unobservable += 1
        elif source:
            escalations["%s->%s" % (source, _tier_of(entry) or "unrouted")] += 1
    # Harnesses that dispatched but never reported a completion: the report
    # states the absence rather than guessing at a cause.
    silent = sorted({e.get("harness") for e in dispatches} - {c.get("harness") for c in completions} - {None})
    pre_instrumentation = sum(1 for e in entries if (e.get("v") or 0) < 3)

    return {
        "records": len(entries),
        "outcomes": {tier: dict(c) for tier, c in sorted(outcomes.items())},
        "verified": {tier: dict(c) for tier, c in sorted(verified.items())},
        "usage": {tier: {**u, "rows": usage_rows[tier]} for tier, u in sorted(usage.items())},
        "escalations": dict(escalations),
        "escalation_unobservable": unobservable,
        "outcome_sources": {h: dict(c) for h, c in sorted(sources.items()) if h},
        "silent_harnesses": silent,
        "joins": joins,
        "pre_instrumentation": pre_instrumentation,
        "dispatch_attempts": len([e for e in dispatches if e.get("decision") != "error"]),
        "window": [entries[0].get("ts"), entries[-1].get("ts")] if entries else [],
        "harnesses": dict(collections.Counter(e.get("harness") for e in entries)),
        "decisions": dict(collections.Counter(e.get("decision") for e in current)),
        "superseded": len(superseded),
        "tiers": dict(tiers),
        "errors": sum(1 for e in entries if e.get("decision") == "error"),
        "blocked": len(blocked),
        "confirmed_executions": len(confirmed),
        "converted": None,  # legacy field: never infer savings from prompt hashes
        "trivial_lone_spawns": len(trivial),
        "agents": dict(collections.Counter(
            "%s @ %s" % (e.get("agent") or "-", e.get("effective_model") or e.get("requested_model") or e.get("model") or "unknown")
            for e in current
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
    if summary.get("superseded"):
        lines.append("  reconciled  %d child(ren) counted once, in their final state" % summary["superseded"])
    lines.append("  harnesses   " + ", ".join("%s %d" % kv for kv in sorted(summary["harnesses"].items())))
    lines.append("  tiers       " + ", ".join("%s %d" % kv for kv in sorted(summary["tiers"].items())))
    if summary["blocked"]:
        lines.append("  blocks      %d (execution/savings not inferred from retries)" % summary["blocked"])
    lines.append("  short-brief signals  %d  (heuristic only; not evidence of wasted spend)" % summary["trivial_lone_spawns"])
    lines.extend(_render_outcomes(summary))
    lines.append("  agent @ model:")
    for name, count in sorted(summary["agents"].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append("    %-44s %d" % (name, count))
    return "\n".join(lines)


def _render_outcomes(summary):
    """Outcome, verification, escalation and usage sections; empty when there is nothing to say."""
    lines = []
    outcomes = summary.get("outcomes") or {}
    if outcomes:
        first = True
        for tier, counts in outcomes.items():
            body = "  ".join("%s %d" % kv for kv in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
            lines.append("  %-11s %-9s %s" % ("outcomes" if first else "", tier, body))
            first = False
    verified = summary.get("verified") or {}
    if verified:
        parts = []
        for tier, counts in verified.items():
            total = sum(counts.values())
            parts.append("%s %d/%d stated evidence" % (tier, counts.get("stated", 0), total))
        lines.append("  verified    " + "; ".join(parts))
    usage = summary.get("usage") or {}
    if usage:
        parts = []
        for tier, u in usage.items():
            parts.append("%s in %d out %d cache-read %d (%d row%s)" % (
                tier, u.get("input", 0), u.get("output", 0), u.get("cache_read", 0), u["rows"], "" if u["rows"] == 1 else "s"))
        lines.append("  child usage " + "; ".join(parts) + "  (tokens summed from observed children only)")
    escalations = summary.get("escalations") or {}
    if escalations or summary.get("escalation_unobservable"):
        body = "; ".join("%s %d" % (k.replace("->", " -> "), v) for k, v in sorted(escalations.items())) or "none observed"
        lines.append("  escalations " + body)
        if summary.get("escalation_unobservable"):
            lines.append("              %d dispatch(es) with an unobservable escalation marker (opaque brief)" % summary["escalation_unobservable"])
    sources = summary.get("outcome_sources") or {}
    silent = summary.get("silent_harnesses") or []
    if sources or silent:
        parts = []
        for harness, counts in sources.items():
            for source, n in sorted(counts.items()):
                note = " (no outcome signal available)" if source == "status-only" else ""
                parts.append("%s %s %d%s" % (harness, source, n, note))
        lines.append("  signals     " + "; ".join(parts) if parts else "  signals")
        for harness in silent:
            lines.append("              %s: no completion signal observed" % harness)
    joins = summary.get("joins") or {}
    if joins:
        lines.append("  joins       " + ", ".join("%s %d" % (k.replace("nearest", "nearest-preceding"), v) for k, v in sorted(joins.items())))
    if summary.get("pre_instrumentation") and outcomes:
        lines.append("  %d record(s) predate outcome signals" % summary["pre_instrumentation"])
    return lines


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

    try:
        rows = read(limit=args.limit)
    except OSError as exc:
        sys.exit("dispatch_log: %s" % exc)
    summary = summarise(rows)
    print(json.dumps(summary, indent=1, sort_keys=True) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
