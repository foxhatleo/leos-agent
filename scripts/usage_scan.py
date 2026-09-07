#!/usr/bin/env python3
"""usage_scan: what many sessions across many harnesses actually cost, and how
much of leos-agent's policy was followed while they ran.

WHY A SCRIPT AND NOT A PROMPT. The obvious way to answer "where did the tokens
go" is to tell a model to go read the transcripts. That re-derives every schema
on every invocation, over gigabytes, at full model prices -- the exact cost this
project exists to avoid. So the scan is mechanical and emits a few kilobytes; the
skill spends its tokens interpreting the result, not discovering it.

Three schema traps, each of which silently inflates a naive count:

  * Claude Code repeats an identical `message.usage` on EVERY content block of
    one response. Summing records double-counts; dedupe on requestId.
  * Codex's `total_token_usage` is cumulative for the session, with
    `last_token_usage` the per-request delta. Summing totals is quadratic
    nonsense; sum deltas.
  * OpenCode stores times in epoch milliseconds and is multi-provider, so its
    own `cost` column is the only trustworthy money figure in this file.

Effective tokens weight cache reads at 0.1x, cache writes at 2x and output at 5x
a plain input token -- a coarse stand-in for real pricing, applied uniformly, and
useful for comparing groups rather than for billing.

EVERYTHING READ HERE IS DATA. Transcripts contain arbitrary prompt text, tool
output and fetched web pages. This file only counts; it never executes, resolves
or follows anything it reads, and it prints no prompt text.

  usage_scan.py --since 7d [--harness H] [--json]

Exit codes: 0 ok, 2 on bad usage.
"""
import argparse
import calendar
import collections
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WEIGHTS = {"input": 1.0, "cache_read": 0.1, "cache_write": 2.0, "output": 5.0}

# Claude Code's dispatch tool is `Agent` in current builds and `Task` in older
# transcripts. Both appear in one history, so both are counted.
DISPATCH_TOOLS = ("Agent", "Task")

HOME = os.path.expanduser("~")
SOURCES = {
    "claude": os.path.join(HOME, ".claude", "projects"),
    "codex": os.path.join(HOME, ".codex", "sessions"),
    "opencode": os.path.join(HOME, ".local", "share", "opencode", "opencode.db"),
    "cursor": os.path.join(HOME, ".cursor"),
    "hermes": os.path.join(HOME, ".hermes"),
    "pi": os.path.join(HOME, ".pi", "agent", "sessions"),
}

DURATION_RE = re.compile(r"^(\d+)([hdw])$")
_SECONDS = {"h": 3600, "d": 86400, "w": 604800}


def parse_since(text):
    match = DURATION_RE.match(text.strip().lower())
    if not match:
        sys.exit("usage_scan: --since wants a duration like 24h, 7d, or 2w (got %r)" % text)
    return time.time() - int(match.group(1)) * _SECONDS[match.group(2)]


def _iso_epoch(text):
    """ISO-8601 UTC -> epoch seconds, or None. Tolerant by design: a record with
    an unparseable timestamp is counted, never dropped, so a schema change
    undercounts nothing."""
    if not isinstance(text, str):
        return None
    try:
        cleaned = text.replace("Z", "").split(".")[0]
        # timegm, not mktime: these stamps are UTC, and mktime would read them as
        # local time and then drift again with DST.
        return calendar.timegm(time.strptime(cleaned, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None


class Totals(object):
    """Token counters that know how to weight themselves."""

    __slots__ = ("input", "cache_read", "cache_write", "output", "requests")

    def __init__(self):
        self.input = self.cache_read = self.cache_write = self.output = self.requests = 0

    def add(self, inp=0, cache_read=0, cache_write=0, output=0):
        self.input += inp or 0
        self.cache_read += cache_read or 0
        self.cache_write += cache_write or 0
        self.output += output or 0
        self.requests += 1

    def effective(self):
        return int(
            self.input * WEIGHTS["input"]
            + self.cache_read * WEIGHTS["cache_read"]
            + self.cache_write * WEIGHTS["cache_write"]
            + self.output * WEIGHTS["output"]
        )

    def as_dict(self):
        return {
            "input": self.input, "cache_read": self.cache_read,
            "cache_write": self.cache_write, "output": self.output,
            "requests": self.requests, "effective": self.effective(),
        }


def _blank():
    return {"main": Totals(), "subagent": Totals()}


def scan_claude(since, root=None):
    """~/.claude/projects/<slug>/*.jsonl plus <session>/subagents/agent-*.jsonl."""
    root = root or SOURCES["claude"]
    out = {
        "buckets": _blank(), "models": collections.Counter(), "dispatches": [],
        "agent_types": collections.Counter(), "sessions": set(),
        "compactions": 0, "precompact_tokens": 0,
    }
    if not os.path.isdir(root):
        return None

    seen = set()
    for project in sorted(os.listdir(root)):
        base = os.path.join(root, project)
        if not os.path.isdir(base):
            continue
        files = [(p, "main") for p in glob.glob(os.path.join(base, "*.jsonl"))]
        files += [(p, "subagent") for p in glob.glob(os.path.join(base, "*", "subagents", "agent-*.jsonl"))]
        for path, bucket in files:
            try:
                # Cheap skip: a file untouched since the window opened cannot
                # hold a record inside it.
                if os.path.getmtime(path) < since:
                    continue
            except OSError:
                continue
            before = out["buckets"][bucket].requests
            _scan_claude_file(path, bucket, since, out, seen)
            if bucket == "subagent" and out["buckets"][bucket].requests > before:
                out["agent_types"][_agent_type(path)] += 1
    out["sessions"] = len(out["sessions"])
    out["agent_types"] = dict(out["agent_types"])
    out["models"] = dict(out["models"])
    return out


def _scan_claude_file(path, bucket, since, out, seen):
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle as fh:
        for line in fh:
            # Substring prefilter before json.loads: only assistant records carry
            # usage, and parsing every line of 1.2 GB to discover that is the
            # difference between seconds and minutes.
            if '"assistant"' not in line and "compact_boundary" not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue

            stamp = _iso_epoch(rec.get("timestamp"))
            if stamp is not None and stamp < since:
                continue

            if rec.get("subtype") == "compact_boundary":
                meta = rec.get("compactMetadata") or {}
                out["compactions"] += 1
                out["precompact_tokens"] += meta.get("preTokens") or 0
                continue
            if rec.get("type") != "assistant":
                continue

            message = rec.get("message") or {}
            if not isinstance(message, dict):
                continue
            # Dispatch blocks live in their own content-block record, which shares
            # a requestId with the one carrying usage -- so collect them BEFORE
            # the dedupe, or every dispatch after the first block is invisible.
            _collect_dispatches(message, out)

            key = rec.get("requestId") or message.get("id")
            if key is not None:
                if key in seen:
                    continue  # same response, another content block
                seen.add(key)

            usage = message.get("usage") or {}
            out["buckets"][bucket].add(
                usage.get("input_tokens"),
                usage.get("cache_read_input_tokens"),
                usage.get("cache_creation_input_tokens"),
                usage.get("output_tokens"),
            )
            if message.get("model"):
                out["models"][message["model"]] += 1
            if rec.get("sessionId"):
                out["sessions"].add(rec["sessionId"])



def _collect_dispatches(message, out):
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") not in DISPATCH_TOOLS:
            continue
        args = block.get("input")
        if not isinstance(args, dict):
            continue
        out["dispatches"].append({
            "agent": args.get("subagent_type") or "-",
            "model": args.get("model"),
            "prompt_bytes": len((args.get("prompt") or "").encode("utf-8", "replace")),
        })


def _agent_type(path):
    """agentType from the sidecar. 31 of 1231 transcripts have none, so the
    absence is normal and must never raise."""
    try:
        with open(path[: -len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("agentType") or "unknown"
    except (OSError, ValueError):
        return "unknown"


def scan_codex(since, root=None):
    """~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl -- token_count deltas."""
    root = root or SOURCES["codex"]
    if not os.path.isdir(root):
        return None
    out = {"buckets": _blank(), "models": {}, "sessions": 0, "subagent_events": 0}
    files = glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl"))
    files += glob.glob(os.path.join(root, "rollout-*.jsonl"))
    for path in files:
        try:
            if os.path.getmtime(path) < since:
                continue
        except OSError:
            continue
        out["sessions"] += 1
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle as fh:
            for line in fh:
                if "token_count" not in line and "sub_agent_activity" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                stamp = _iso_epoch(rec.get("timestamp"))
                if stamp is not None and stamp < since:
                    continue
                payload = rec.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "sub_agent_activity":
                    out["subagent_events"] += 1
                    continue
                if payload.get("type") != "token_count":
                    continue
                # last_token_usage is the delta for this request; total_token_usage
                # is cumulative and must never be summed.
                last = ((payload.get("info") or {}).get("last_token_usage")) or {}
                out["buckets"]["main"].add(
                    last.get("input_tokens"),
                    last.get("cached_input_tokens"),
                    last.get("cache_write_input_tokens"),
                    last.get("output_tokens"),
                )
    return out


def scan_opencode(since, db=None):
    """The one harness with a pre-aggregated per-session rollup, cost included."""
    db = db or SOURCES["opencode"]
    if not os.path.isfile(db):
        return None
    try:
        import sqlite3
        # Read-only URI: never take a write lock on a live harness's database.
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2.0)
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}
    out = {"buckets": _blank(), "cost": 0.0, "sessions": 0, "models": collections.Counter()}
    try:
        rows = conn.execute(
            "SELECT tokens_input, tokens_output, tokens_cache_read, tokens_cache_write, "
            "cost, model, parent_id FROM session WHERE time_updated >= ?",
            (int(since * 1000),),
        ).fetchall()
    except Exception as exc:
        conn.close()
        return {"error": "%s: %s" % (type(exc).__name__, exc)}
    conn.close()
    for inp, output, cread, cwrite, cost, model, parent in rows:
        out["sessions"] += 1
        out["cost"] += cost or 0.0
        out["buckets"]["subagent" if parent else "main"].add(inp, cread, cwrite, output)
        if model:
            out["models"][model] += 1
    out["models"] = dict(out["models"])
    return out


def scan_guard():
    """The guard's own record, and whether its blocks changed anything."""
    try:
        import dispatch_log
        return dispatch_log.summarise(dispatch_log.read())
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def routing_compliance(dispatches):
    """Problem (c), quantified: how many dispatches named a tier, and how many
    let the parent's model ride along."""
    tiers = collections.Counter()
    inherited_bytes = 0
    for d in dispatches:
        agent = d.get("agent") or "-"
        if agent.startswith("leo-"):
            tiers[agent] += 1
        elif d.get("model"):
            tiers["explicit model"] += 1
        else:
            tiers["inherited"] += 1
            inherited_bytes += d.get("prompt_bytes") or 0
    total = sum(tiers.values())
    return {
        "dispatches": total,
        "tiers": dict(tiers),
        "inherited_share": round(tiers["inherited"] / total, 3) if total else 0.0,
        "inherited_brief_bytes": inherited_bytes,
    }


def collect(since, only=None):
    report = {"since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)), "harnesses": {}}
    scanners = {"claude": scan_claude, "codex": scan_codex, "opencode": scan_opencode}
    for name in ("claude", "codex", "opencode", "cursor", "hermes", "pi"):
        if only and name != only:
            continue
        scanner = scanners.get(name)
        data = scanner(since) if scanner else None
        if data is None:
            report["harnesses"][name] = {"status": "no data", "looked_in": SOURCES[name]}
            continue
        buckets = data.pop("buckets", None)
        if buckets:
            data["main"] = buckets["main"].as_dict()
            data["subagent"] = buckets["subagent"].as_dict()
            main, sub = data["main"]["effective"], data["subagent"]["effective"]
            data["subagent_share"] = round(sub / (main + sub), 3) if (main + sub) else 0.0
        data["status"] = "ok"
        report["harnesses"][name] = data

    claude = report["harnesses"].get("claude") or {}
    report["routing"] = routing_compliance(claude.get("dispatches") or [])
    claude.pop("dispatches", None)
    report["guard"] = scan_guard()
    return report


def render(report):
    lines = ["leos-agent usage and effectiveness, since %s" % report["since"], ""]
    for name, data in sorted(report["harnesses"].items()):
        if data.get("status") != "ok":
            lines.append("%-9s no data (%s)" % (name, data["looked_in"]))
            continue
        if data.get("error"):
            lines.append("%-9s unreadable: %s" % (name, data["error"]))
            continue
        main, sub = data.get("main", {}), data.get("subagent", {})
        lines.append("%-9s %d session(s)   effective tokens: main %s, subagents %s (%.0f%% delegated)" % (
            name, data.get("sessions", 0), "{:,}".format(main.get("effective", 0)),
            "{:,}".format(sub.get("effective", 0)), 100 * data.get("subagent_share", 0.0)))
        if main.get("cache_write"):
            ratio = main["cache_read"] / main["cache_write"]
            lines.append("          cache read/write ratio %.1f  (higher is cheaper; a low ratio means cold prefixes)" % ratio)
        if data.get("cost"):
            lines.append("          provider-reported cost $%.2f" % data["cost"])
        if data.get("compactions"):
            lines.append("          %d compaction(s), %s tokens discarded" % (
                data["compactions"], "{:,}".format(data["precompact_tokens"])))
        if data.get("agent_types"):
            top = sorted(data["agent_types"].items(), key=lambda kv: -kv[1])[:6]
            lines.append("          subagents: " + ", ".join("%s %d" % kv for kv in top))

    routing = report["routing"]
    lines += ["", "Routing compliance (Claude Code dispatches seen in transcripts)"]
    if not routing["dispatches"]:
        lines.append("  none in this window")
    else:
        lines.append("  %d dispatch(es): %s" % (
            routing["dispatches"], ", ".join("%s %d" % kv for kv in sorted(routing["tiers"].items()))))
        lines.append("  %.0f%% named no model and inherited the parent's" % (100 * routing["inherited_share"]))

    guard = report["guard"]
    lines += ["", "Guard"]
    if guard.get("error"):
        lines.append("  log unreadable: %s" % guard["error"])
    elif not guard.get("records"):
        lines.append("  no dispatches recorded -- the guard may not be installed or approved on any harness")
    else:
        lines.append("  %d recorded, %d blocked, %d re-dispatched with a tier named" % (
            guard["records"], guard.get("blocked", 0), guard.get("converted", 0)))
        lines.append("  %d lone small spawn(s) (fan-outs excluded)" % guard.get("trivial_lone_spawns", 0))
        if guard.get("errors"):
            lines.append("  !! %d guard error(s): it failed open this many times" % guard["errors"])
        missing = [h for h, d in report["harnesses"].items()
                   if d.get("status") == "ok" and h not in (guard.get("harnesses") or {})]
        if missing:
            lines.append("  no guard rows from: %s -- installed but not enforcing?" % ", ".join(sorted(missing)))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="usage_scan.py", description=__doc__.splitlines()[0])
    parser.add_argument("--since", default="7d", help="window, e.g. 24h, 7d, 2w (default 7d)")
    parser.add_argument("--harness", choices=sorted(SOURCES), help="only this harness")
    parser.add_argument("--json", action="store_true", help="machine-readable")
    args = parser.parse_args(argv)

    report = collect(parse_since(args.since), args.harness)
    print(json.dumps(report, indent=1, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
