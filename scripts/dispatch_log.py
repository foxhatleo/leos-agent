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
import re
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

# v3 added tier, escalation_from, outcome, verified, outcome_source, usage;
# completion rows may also carry turns, a count of the child's model requests.
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


def _lines(candidate):
    """(lines, (device, inode)) of one file; ([], None) when it does not exist."""
    try:
        with open(candidate, encoding="utf-8", errors="replace") as fh:
            stat = os.fstat(fh.fileno())
            return fh.read().splitlines(), (stat.st_dev, stat.st_ino)
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        # Raise, don't exit: read() serves hooks and scanners that must file
        # an unreadable log as a finding and carry on. A SystemExit here
        # skipped a Codex lifecycle hook's mandatory JSON reply and took the
        # diagnosis bundle down before it wrote anything. The CLI exits in main().
        raise OSError("%s: %s" % (candidate, exc.strerror or exc)) from exc


def _identity(candidate):
    try:
        stat = os.stat(candidate)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def read(limit=None, target=None):
    """Records, oldest first. Tolerates a truncated final line from a crash.

    Lock-free, so a hook never waits on a reader. A rotation that lands between
    reading the old generation and the current one would drop the generation
    in between; the current file's identity is compared before and after, and
    the pair is read again when it changed.
    """
    if target:
        lines = _lines(target)[0]
    else:
        current = path()
        for _attempt in range(3):
            before = _identity(current)
            older = _lines(current + ".1")[0]
            newer, after = _lines(current)
            if after == before:
                break
        lines = older + newer
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a half-written last line, or a foreign line
        if isinstance(entry, dict):
            out.append(entry)
    return out[-limit:] if limit else out


COMPLETION = ("executed", "completed")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")


def _token(value):
    """An enum-shaped log value as it is; anything else, free text included, as `other`."""
    if value is None:
        return "none"
    return value if isinstance(value, str) and _TOKEN_RE.fullmatch(value) else "other"


def _tier_of(row):
    """A dispatch or completion row's tier: v3 records it; older rows recompute."""
    from routing_engine import tier_for
    return row.get("tier") or tier_for(row.get("agent") or "") or None


def _ts(row):
    return row.get("ts") or ""


def _ran(entry):
    """A dispatch the guard let through. Blocks and guard errors spent nothing."""
    return (entry.get("decision") not in COMPLETION + ("block", "error")
            and bool(entry.get("agent") or entry.get("tool")))


def _child_key(row):
    """One child's identity across its reports; None when it has none.

    Only a call id or the child's own id identifies a child. Agent name plus
    a timestamp bucket does not: siblings of one agent finishing in the same
    ten seconds are different children, not one child reported twice.
    """
    if not (row.get("call_id") or row.get("agent_id")):
        return None
    return row.get("harness"), row.get("session"), row.get("call_id"), row.get("agent_id")


def _children(entries):
    """Completion rows, one per child, oldest first.

    A child reported more than once -- a SubagentStop row and the SessionEnd
    row that backfilled it, or a stop that another hook kept running -- is
    merged in log order, so later known values win and an `unknown` never
    erases an earlier outcome.
    """
    children, index = [], {}
    for row in sorted((e for e in entries if e.get("decision") in COMPLETION), key=_ts):
        key = _child_key(row)
        if key is None or key not in index:
            if key is not None:
                index[key] = len(children)
            children.append(row)
            continue
        prior = children[index[key]]
        merged = dict(prior)
        merged.update({k: v for k, v in row.items() if v not in (None, "unknown")})
        if "executed" in (prior.get("decision"), row.get("decision")):
            merged["decision"] = "executed"
        merged["ts"] = _ts(prior) or _ts(row)  # the first stop is when the child finished
        children[index[key]] = merged
    return children


def join(entries):
    """Pair each completion with the dispatch it finished. Read time only.

    Order of evidence: call_id; else, for a completion that carries no call
    id, the nearest preceding same-session dispatch that ran, of the same tier
    (an untiered completion takes only an untiered dispatch or one naming the
    same agent); else unmatched. Each dispatch is claimed once, except that
    children sharing one call id are one batched dispatch. A tie at the
    nearest step is `ambiguous`, and an ambiguous outcome is never credited to
    a real tier. Claude's SubagentStop carries no call id, so Claude joins are
    always the nearest-preceding guess.
    """
    dispatches = [e for e in entries if _ran(e)]
    completions = _children(entries)
    by_call = collections.defaultdict(list)
    for d in dispatches:
        if d.get("call_id"):
            by_call[(d.get("harness"), d.get("session"), d["call_id"])].append(d)
    claimed = set()
    pairs = {}
    stats = collections.Counter()
    for row in completions:
        named = by_call.get((row.get("harness"), row.get("session"), row.get("call_id")), []) if row.get("call_id") else []
        candidates = [d for d in named if id(d) not in claimed]
        how = "call_id" if candidates else None
        if named and not candidates:
            # A sibling from the same call already claimed it: one batch.
            stats["call_id"] += 1
            pairs[id(row)] = (named[-1], _tier_of(named[-1]) or "unrouted", "call_id")
            continue
        if not candidates and not row.get("call_id"):
            want = _tier_of(row)
            preceding = [d for d in dispatches if id(d) not in claimed and d.get("harness") == row.get("harness")
                         and d.get("session") == row.get("session") and _ts(d) <= _ts(row)
                         and (_tier_of(d) == want if want is not None
                              else _tier_of(d) is None or (bool(row.get("agent")) and d.get("agent") == row.get("agent")))]
            if preceding:
                latest = max(_ts(d) for d in preceding)
                candidates = [d for d in preceding if _ts(d) == latest]
                how = "nearest"
        if not candidates:
            stats["unmatched"] += 1
            pairs[id(row)] = (None, _tier_of(row) or "unrouted", "unmatched")
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


def _reference_usd(model, usage, catalog):
    """(low, high) reference USD for one child's tokens, or None when unpriced.

    The same rates and conditional-rate range the usage scan applies. A
    reference estimate from public catalog prices, never a bill.
    """
    import pricing
    if not isinstance(model, str) or not model:
        return None
    match = pricing.resolve(model, catalog)
    if match.model is None:
        return None
    rows = [match.pricing] + list(match.pricing.get("overrides", []))
    low = high = 0
    for field, rate_key in (("input", "prompt"), ("cache_read", "input_cache_read"),
                            ("cache_write", "input_cache_write"), ("output", "completion")):
        amount = usage.get(field) or 0
        if not amount:
            continue
        rates = [pricing.decimal(row.get(rate_key, match.pricing.get(rate_key))) for row in rows]
        if None in rates:
            return None
        low += amount * min(rates)
        high += amount * max(rates)
    return float(low), float(high)


def _costs(completions, pairs, catalog):
    """Reference cost per verified success, per tier, over the children that can be priced."""
    out = {}
    for row in completions:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            continue
        if catalog is None:
            import pricing
            catalog = pricing.load()
        tier = pairs[id(row)][1]
        bucket = out.setdefault(tier, {"reference_usd": [0.0, 0.0], "priced_runs": 0, "unpriced_runs": 0,
                                       "verified_successes": 0, "per_verified_success_usd": None})
        price = _reference_usd(row.get("effective_model"), usage, catalog)
        if price is None:
            bucket["unpriced_runs"] += 1
            continue
        bucket["priced_runs"] += 1
        bucket["reference_usd"] = [bucket["reference_usd"][0] + price[0], bucket["reference_usd"][1] + price[1]]
        if row.get("outcome") == "done" and row.get("verified") is True:
            bucket["verified_successes"] += 1
    for bucket in out.values():
        wins = bucket["verified_successes"]
        if wins:
            bucket["per_verified_success_usd"] = [bucket["reference_usd"][0] / wins, bucket["reference_usd"][1] / wins]
    return out


def summarise(entries, catalog=None):
    """The read-time judgment: burst collapse, tiers, and block conversion."""
    # A child reported twice -- the SubagentStop "completed" row and the
    # SessionEnd "executed" row that supersedes it -- is counted once, in its
    # final state, so decisions and agents describe children rather than
    # rows. records stays the raw retained count.
    last_report = {}
    for e in entries:
        if e.get("decision") in COMPLETION and e.get("session") and _child_key(e):
            last_report[_child_key(e)] = id(e)
    superseded = {
        id(e) for e in entries
        if e.get("decision") in COMPLETION and e.get("session") and _child_key(e)
        and last_report[_child_key(e)] != id(e)
    }
    current = [e for e in entries if id(e) not in superseded]
    dispatches = [e for e in entries if e.get("decision") not in ("executed", "completed")]
    ran = [e for e in dispatches if e.get("decision") not in ("block", "error")]
    # Allowed attempts count toward a burst; execution is not established. A block and the
    # re-dispatch it forced land in the same two-second bucket, and counting
    # both would let every blocked retry pose as a fan-out of two.
    bursts = collections.Counter(e.get("burst") for e in ran if e.get("burst"))

    # Tiers and escalations count dispatches that ran. A block and the retry
    # it forced are one piece of work; blocks are reported on their own line.
    tiers = collections.Counter()
    for entry in ran:
        agent = entry.get("agent") or ""
        if is_tier(agent):
            tiers[agent] += 1
        elif entry.get("model"):
            tiers["explicit model"] += 1
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
    confirmed = [e for e in current if e.get("decision") == "executed"]

    completions, pairs, joins = join(entries)
    outcomes = {}
    verified = {}
    usage = {}
    usage_rows = collections.Counter()
    turns = {}
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
        if isinstance(row.get("turns"), int) and not isinstance(row.get("turns"), bool) and row["turns"] > 0:
            bucket = turns.setdefault(tier, {"total": 0, "rows": 0})
            bucket["total"] += row["turns"]
            bucket["rows"] += 1
    # A dispatch that ran but that no completion claimed has no completion
    # signal: a background child whose stop event never fired, a harness that
    # reports nothing, or a child still running. Counted per tier beside the
    # outcomes rather than silently left out of them.
    claimed = {id(chosen) for chosen, _tier, _how in pairs.values() if chosen is not None}
    coverage = {}
    for entry in dispatches:
        if not _ran(entry) or (entry.get("v") or 0) < 3:
            continue
        bucket = coverage.setdefault(_tier_of(entry) or "unrouted", {"ran": 0, "no_signal": 0})
        bucket["ran"] += 1
        bucket["no_signal"] += id(entry) not in claimed
    escalations = collections.Counter()
    unobservable = 0
    for entry in ran:
        source = entry.get("escalation_from")
        if source == "unobservable":
            unobservable += 1
        elif source:
            escalations["%s->%s" % (source, _tier_of(entry) or "unrouted")] += 1
    # Diagnostic tokens the guard stamps on a dispatch, such as a routing
    # config that failed validation. Enum-shaped values are counted as they
    # are; anything else is counted without being echoed.
    diagnostics = collections.Counter(
        _token(e["diagnostic"]) for e in dispatches if isinstance(e.get("diagnostic"), str) and e["diagnostic"]
    )
    # Why the guard decided as it did, per decision. Older error rows put the
    # exception text in `reason`; that groups as `other`, never echoed.
    reasons = collections.defaultdict(collections.Counter)
    for e in dispatches:
        reasons[_token(e.get("decision"))][_token(e.get("reason"))] += 1
    error_types = collections.Counter(
        e["error_type"] if isinstance(e.get("error_type"), str) and _ERROR_TYPE_RE.fullmatch(e["error_type"]) else "other"
        for e in dispatches if e.get("decision") == "error" and e.get("error_type") is not None
    )
    # Harnesses that dispatched but never reported a completion: the report
    # states the absence rather than guessing at a cause.
    silent = sorted({e.get("harness") for e in dispatches} - {c.get("harness") for c in completions} - {None})
    pre_instrumentation = sum(1 for e in entries if (e.get("v") or 0) < 3)

    return {
        "records": len(entries),
        "outcomes": {tier: dict(c) for tier, c in sorted(outcomes.items())},
        "verified": {tier: dict(c) for tier, c in sorted(verified.items())},
        "usage": {tier: {**u, "rows": usage_rows[tier]} for tier, u in sorted(usage.items())},
        "turns": dict(sorted(turns.items())),
        "cost": dict(sorted(_costs(completions, pairs, catalog).items())),
        "coverage": dict(sorted(coverage.items())),
        "diagnostics": dict(sorted(diagnostics.items())),
        "reasons": {decision: dict(sorted(c.items())) for decision, c in sorted(reasons.items())},
        "error_types": dict(sorted(error_types.items())),
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
    # A row from before harness tagging has no harness; it must not break the sort.
    lines.append("  harnesses   " + ", ".join("%s %d" % kv for kv in sorted(summary["harnesses"].items(), key=lambda kv: str(kv[0]))))
    lines.append("  tiers       " + ", ".join("%s %d" % kv for kv in sorted(summary["tiers"].items())))
    if summary["blocked"]:
        lines.append("  blocks      %d (execution/savings not inferred from retries)" % summary["blocked"])
    if summary.get("diagnostics"):
        lines.append("  diagnostics " + ", ".join("%s %d" % kv for kv in summary["diagnostics"].items()))
    first = True
    for decision, counts in (summary.get("reasons") or {}).items():
        body = ", ".join("%s %d" % kv for kv in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        lines.append("  %-11s %s: %s" % ("reasons" if first else "", decision, body))
        first = False
    if summary.get("error_types"):
        lines.append("  error types " + ", ".join("%s %d" % kv for kv in summary["error_types"].items()))
    lines.append("  short-brief signals  %d  (heuristic only; not evidence of wasted spend)" % summary["trivial_lone_spawns"])
    lines.extend(_render_outcomes(summary))
    lines.append("  agent @ model:")
    for name, count in sorted(summary["agents"].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append("    %-44s %d" % (name, count))
    return "\n".join(lines)


def _usd(pair):
    low, high = pair
    return "$%.4f" % low if round(low, 4) == round(high, 4) else "$%.4f-$%.4f" % (low, high)


def _render_outcomes(summary):
    """Outcome, verification, escalation and usage sections; empty when there is nothing to say."""
    lines = []
    outcomes = summary.get("outcomes") or {}
    silent_tiers = {tier: c["no_signal"] for tier, c in (summary.get("coverage") or {}).items() if c.get("no_signal")}
    first = True
    for tier in sorted(set(outcomes) | set(silent_tiers)):
        counts = outcomes.get(tier, {})
        body = "  ".join("%s %d" % kv for kv in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        if silent_tiers.get(tier):
            body = (body + "  | " if body else "| ") + "no completion signal %d" % silent_tiers[tier]
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
    turns = summary.get("turns") or {}
    if turns:
        lines.append("  turns       " + "; ".join("%s %d over %d run%s (mean %.1f)" % (
            tier, t["total"], t["rows"], "" if t["rows"] == 1 else "s", t["total"] / t["rows"]) for tier, t in turns.items()))
    cost = summary.get("cost") or {}
    if cost:
        parts = []
        for tier, c in cost.items():
            spent = _usd(c["reference_usd"])
            if c["per_verified_success_usd"]:
                part = "%s %s per verified success (%s over %d priced run%s, %d verified)" % (
                    tier, _usd(c["per_verified_success_usd"]), spent, c["priced_runs"],
                    "" if c["priced_runs"] == 1 else "s", c["verified_successes"])
            elif c["priced_runs"]:
                part = "%s no verified success (%s over %d priced run%s)" % (
                    tier, spent, c["priced_runs"], "" if c["priced_runs"] == 1 else "s")
            else:
                part = "%s unpriced" % tier
            if c["unpriced_runs"]:
                part += ", %d unpriced" % c["unpriced_runs"]
            parts.append(part)
        lines.append("  cost        " + "; ".join(parts))
        lines.append("              (reference estimate from catalog prices, not a bill)")
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
