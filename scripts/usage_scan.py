#!/usr/bin/env python3
"""Count observed local usage without loading transcripts into a model.

Reports disjoint token categories and OpenRouter reference-price ranges, not
bills or proven savings. Unknown schemas, models, timestamps, and cache rates
remain explicit gaps. No prompt text is emitted or executed.
"""
import argparse
import datetime
import hashlib
import collections
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TOKEN_KEYS = ("input", "cache_read", "cache_write", "output")
# Rows a harness writes for its own bookkeeping, not a model anyone is billed
# for. Claude Code's "<synthetic>" assistant rows carry no usage; report them
# as internal rather than as an unknown model with an unpriced subtotal.
INTERNAL_MODELS = frozenset(("<synthetic>",))

# Claude Code's dispatch tool is `Agent` in current builds and `Task` in older
# transcripts. Both appear in one history, so both are counted.
DISPATCH_TOOLS = ("Agent", "Task")

HOME = os.path.expanduser("~")
SOURCES = {
    "claude": os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", os.path.join(HOME, ".claude")), "projects"),
    "codex": os.path.join(os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex")), "sessions"),
    "opencode": os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share")), "opencode", "opencode.db"),
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
    if not isinstance(text, str):
        return None
    try:
        stamp = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    # Every harness here writes UTC. Reading a naive stamp as UTC restores the
    # old rule -- a record is counted, never dropped -- because returning None
    # excludes it from the window entirely, and a harness that stopped writing
    # the offset would report zero usage rather than a diagnostic.
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class Totals:
    def __init__(self):
        self.input = self.cache_read = self.cache_write = self.output = self.requests = 0

    def add(self, inp=0, cache_read=0, cache_write=0, output=0):
        for key, value in zip(TOKEN_KEYS, (inp, cache_read, cache_write, output)):
            setattr(self, key, getattr(self, key) + count(value))
        self.requests += 1

    def raw(self):
        return sum(getattr(self, key) for key in TOKEN_KEYS)

    def as_dict(self):
        return {**{key: getattr(self, key) for key in TOKEN_KEYS}, "requests": self.requests, "total": self.raw()}


def _blank():
    return {"main": Totals(), "subagent": Totals()}


def new_scan():
    return {"buckets": _blank(), "models": collections.Counter(), "model_usage": {},
            "sessions": set(), "diagnostics": collections.Counter()}


def add_usage(out, bucket, model, values, session):
    model = model if isinstance(model, str) and model else "unknown"
    out["buckets"][bucket].add(*values)
    out["model_usage"].setdefault(model, _blank())[bucket].add(*values)
    out["models"][model] += 1
    out["sessions"].add(session)


def finish(out):
    out["sessions"] = len(out["sessions"])
    out["models"] = dict(out["models"])
    out["diagnostics"] = dict(out["diagnostics"])
    out["model_usage"] = {model: {role: total.as_dict() for role, total in buckets.items()}
                          for model, buckets in out["model_usage"].items()}
    return out


def records(path, out):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        yield rec
                    else:
                        out["diagnostics"]["invalid_records"] += 1
                except ValueError:
                    out["diagnostics"]["invalid_records"] += 1
    except OSError:
        out["diagnostics"]["unreadable_files"] += 1


def in_window(rec, since, out):
    stamp = _iso_epoch(rec.get("timestamp"))
    if stamp is None:
        out["diagnostics"]["unknown_timestamp_records"] += 1
        return False
    return stamp >= since


def scan_claude(since, root=None):
    """~/.claude/projects/<slug>/*.jsonl plus <session>/subagents/agent-*.jsonl."""
    root = root or SOURCES["claude"]
    out = new_scan()
    out.update({"dispatches": [], "agent_types": collections.Counter(),
                "compactions": 0, "precompact_tokens": 0})
    if not os.path.isdir(root):
        return None

    seen, dispatch_seen = {}, set()
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
            before = len(seen)
            _scan_claude_file(path, bucket, since, out, seen, dispatch_seen)
            if bucket == "subagent" and len(seen) > before:
                out["agent_types"][_agent_type(path)] += 1
    for bucket, model, values, session in seen.values():
        add_usage(out, bucket, model, values, session)
    out["agent_types"] = dict(out["agent_types"])
    return finish(out)


def _scan_claude_file(path, bucket, since, out, seen, dispatch_seen):
    for index, rec in enumerate(records(path, out)):
        if rec.get("type") != "assistant" and rec.get("subtype") != "compact_boundary":
            continue
        if not in_window(rec, since, out):
            continue
        if rec.get("subtype") == "compact_boundary":
            out["compactions"] += 1
            out["precompact_tokens"] += count((rec.get("compactMetadata") or {}).get("preTokens"))
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        key = rec.get("requestId") or message.get("id") or (path, index)
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") not in DISPATCH_TOOLS:
                continue
            args = block.get("input")
            if not isinstance(args, dict):
                continue
            block_id = block.get("id") or hashlib.sha256(json.dumps(block, sort_keys=True).encode()).hexdigest()
            dispatch_key = (str(key), block_id)
            if dispatch_key in dispatch_seen:
                continue
            dispatch_seen.add(dispatch_key)
            prompt = args.get("prompt")
            out["dispatches"].append({"agent": args.get("subagent_type") or "-", "model": args.get("model"),
                                      "prompt_bytes": len(prompt.encode("utf-8", "replace")) if isinstance(prompt, str) else 0})
        usage = message.get("usage")
        if not isinstance(usage, dict) or not usage:
            continue
        values = tuple(count(usage.get(k)) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
        if key in seen:
            old_bucket, model, previous, session = seen[key]
            # Streaming snapshots are cumulative per response, not new requests.
            # Keep the greatest observed counter for each category.
            values = tuple(max(a, b) for a, b in zip(previous, values))
            seen[key] = old_bucket, model, values, session
        else:
            seen[key] = bucket, message.get("model"), values, rec.get("sessionId") or path



def _agent_type(path):
    """agentType from the sidecar. 31 of 1231 transcripts have none, so the
    absence is normal and must never raise."""
    try:
        with open(path[: -len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("agentType") or "unknown"
    except (OSError, ValueError):
        return "unknown"


def scan_codex(since, root=None):
    root = root or SOURCES["codex"]
    roots = [root]
    if os.path.basename(os.path.normpath(root)) == "sessions":
        roots.append(os.path.join(os.path.dirname(os.path.normpath(root)), "archived_sessions"))
    if not any(os.path.isdir(directory) for directory in roots):
        return None
    out = new_scan()
    out["subagent_events"] = 0
    keys = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
    paths = {path for directory in roots for path in glob.glob(os.path.join(directory, "**", "rollout-*.jsonl"), recursive=True)}
    out["coverage"] = "Local rollout JSONL files in sessions and archived_sessions; other history formats are not measured."
    for path in sorted(paths):
        previous, model, bucket = None, None, "main"
        for rec in records(path, out):
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                continue
            if rec.get("type") == "session_meta":
                source = payload.get("source")
                if isinstance(source, dict) and "subagent" in source:
                    bucket = "subagent"
                continue
            if rec.get("type") == "turn_context":
                model = payload.get("model") or model
                continue
            if payload.get("type") == "sub_agent_activity":
                if in_window(rec, since, out):
                    out["subagent_events"] += 1
                continue
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            total, last = info.get("total_token_usage"), info.get("last_token_usage")
            if not isinstance(total, dict):
                out["diagnostics"]["missing_cumulative_usage"] += 1
                continue  # repeated last-only events cannot be safely deduplicated
            current = tuple(count(total.get(k)) for k in keys)
            if current == previous:
                continue
            if previous is not None and all(a >= b for a, b in zip(current, previous)):
                values = tuple(a - b for a, b in zip(current, previous))
            elif isinstance(last, dict):
                values = tuple(count(last.get(k)) for k in keys)
                if previous is not None:
                    out["diagnostics"]["cumulative_resets"] += 1
            else:
                previous = current
                out["diagnostics"]["missing_initial_delta"] += 1
                continue
            previous = current  # even pre-window events establish the baseline
            if not in_window(rec, since, out) or not any(values):
                continue
            inp, read, write, output = values
            if read + write > inp:
                out["diagnostics"]["cache_exceeds_input"] += 1
            # Codex input includes cached input; output already includes reasoning.
            add_usage(out, bucket, model, (max(0, inp - read - write), read, write, output), path)
    return finish(out)


def scan_opencode(since, db=None):
    """Use message-time usage, never whole-session totals filtered by last update."""
    db = db or SOURCES["opencode"]
    if not os.path.isfile(db):
        return None
    import sqlite3
    from urllib.parse import quote
    out = new_scan()
    out.update({"reported_cost_usd": 0.0, "reported_cost_records": 0})
    try:
        conn = sqlite3.connect("file:" + quote(os.path.abspath(db)) + "?mode=ro", uri=True, timeout=2)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            supported = [table for table in ("session_message", "message") if table in tables]
            if not supported:
                raise sqlite3.OperationalError("no supported message usage table")
            seen = set()
            for table in supported:
                # New store first, then v1 mirrors with the same message IDs.
                type_column = "m.type" if table == "session_message" else "NULL"
                rows = conn.execute("SELECT m.id, m.session_id, m.data, s.parent_id, " + type_column + " FROM " + table + " m "
                                    "JOIN session s ON m.session_id=s.id WHERE m.time_created >= ?",
                                    (int(since * 1000),))
                for message_id, session, raw, parent, stored_type in rows:
                    if message_id in seen:
                        continue
                    try:
                        message = json.loads(raw)
                    except (ValueError, TypeError):
                        out["diagnostics"]["invalid_records"] += 1
                        continue
                    if not isinstance(message, dict):
                        continue
                    role = message.get("type") if table == "session_message" else message.get("role")
                    # In v2 the type is a separate column, omitted from data.
                    if table == "session_message":
                        role = stored_type
                    if role != "assistant":
                        continue
                    tokens = message.get("tokens")
                    if not isinstance(tokens, dict):
                        out["diagnostics"]["missing_usage"] += 1
                        continue
                    seen.add(message_id)
                    cache = tokens.get("cache") or {}
                    model_ref = message.get("model") or {}
                    model = message.get("modelID") or model_ref.get("id")
                    provider = message.get("providerID") or model_ref.get("providerID")
                    if model and provider and "/" not in model:
                        model = provider + "/" + model
                    # OpenCode stores output excluding reasoning; sum them once.
                    output = count(tokens.get("output")) + count(tokens.get("reasoning"))
                    add_usage(out, "subagent" if parent else "main", model,
                              (tokens.get("input"), cache.get("read"), cache.get("write"), output), session)
                    cost = message.get("cost")
                    if isinstance(cost, (float, int)) and not isinstance(cost, bool) and 0 <= cost < float("inf"):
                        out["reported_cost_usd"] += cost
                        out["reported_cost_records"] += 1
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        return {"error": str(exc), "status": "schema-unavailable"}
    return finish(out)


def scan_guard(since=0, only=None):
    """The guard's own record, and whether its blocks changed anything."""
    try:
        import dispatch_log
        return dispatch_log.summarise([row for row in dispatch_log.read()
                                       if (_iso_epoch(row.get("ts")) or 0) >= since
                                       and (not only or row.get("harness") == only)])
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def _is_tier(agent):
    """Shared with the guard, so the report cannot disagree with enforcement
    about what counts as a tier -- namespaced plugin types included."""
    try:
        import dispatch_log
        return dispatch_log.is_tier(agent)
    except Exception:
        return False


def routing_compliance(dispatches):
    """Problem (c), quantified: how many dispatches named a tier, and how many
    let the parent's model ride along."""
    tiers = collections.Counter()
    inherited_bytes = 0
    for d in dispatches:
        agent = d.get("agent") or "-"
        if _is_tier(agent):
            tiers[agent] += 1
        elif d.get("model"):
            tiers["explicit model"] += 1
        else:
            tiers["model unspecified"] += 1
            inherited_bytes += d.get("prompt_bytes") or 0
    total = sum(tiers.values())
    return {
        "dispatches": total,
        "tiers": dict(tiers),
        "unspecified_share": round(tiers["model unspecified"] / total, 3) if total else 0.0,
        "unspecified_brief_bytes": inherited_bytes,
    }


def reference_cost(model, buckets, catalog):
    import pricing
    from decimal import Decimal
    amounts = {key: sum(role[key] for role in buckets.values()) for key in TOKEN_KEYS}
    if model in INTERNAL_MODELS:
        return {"requested": model, "status": "internal", "reference_model": None,
                "minimum_usd": 0.0, "maximum_usd": 0.0, "unpriced_tokens": sum(amounts.values())}
    match = pricing.resolve(model, catalog)
    result = {**match.report(), "minimum_usd": 0.0, "maximum_usd": 0.0, "unpriced_tokens": 0}
    low, high = Decimal(0), Decimal(0)
    for key, rate_key in zip(TOKEN_KEYS, ("prompt", "input_cache_read", "input_cache_write", "completion")):
        if not amounts[key]:
            continue
        rates = [match.pricing] + match.pricing.get("overrides", [])
        values = [pricing.decimal(row.get(rate_key, match.pricing.get(rate_key))) for row in rates]
        if None in values:
            result["unpriced_tokens"] += amounts[key]
            continue
        low += amounts[key] * min(values)
        high += amounts[key] * max(values)
    result.update(minimum_usd=float(low), maximum_usd=float(high))
    return result


def collect(since, only=None):
    import pricing
    catalog = pricing.load()
    report = {"since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)), "harnesses": {},
              "pricing": {"source": catalog.get("source"), "fetched_at": catalog.get("fetched_at"),
                          "basis": "Current reference text-token rates; conditional rates shown as ranges. Not historical bills. Excludes non-token fees and negotiated/subscription pricing."},
              "savings": {"status": "not measured", "reason": "No comparable task baseline; delegation share is not savings."}}
    scanners = {"claude": scan_claude, "codex": scan_codex, "opencode": scan_opencode}
    for name in SOURCES:
        if only and name != only:
            continue
        scanner = scanners.get(name)
        if scanner is None:
            report["harnesses"][name] = {"status": "unsupported", "reason": "Local usage schema not supported; installation status unknown."}
            continue
        data = scanner(since)
        if data is None:
            report["harnesses"][name] = {"status": "no data", "looked_in": SOURCES[name]}
            continue
        if data.get("error"):
            data["status"] = "error"
        else:
            buckets = data.pop("buckets")
            data.update({role: total.as_dict() for role, total in buckets.items()})
            main, sub = buckets["main"].raw(), buckets["subagent"].raw()
            data["subagent_token_share"] = round(sub / (main + sub), 3) if main + sub else 0
            data["reference_cost"] = {model: reference_cost(model, usage, catalog) for model, usage in data["model_usage"].items()}
            data["status"] = "partial" if data.get("diagnostics") else "ok"
        report["harnesses"][name] = data
    claude = report["harnesses"].get("claude") or {}
    report["routing"] = routing_compliance(claude.pop("dispatches", []))
    report["guard"] = scan_guard(since, only)
    return report


def render(report):
    lines = ["leos-agent observed usage since " + report["since"],
             "Reference costs are not bills. Savings require a comparable task baseline.", ""]
    # Sorted, so the text is the same whether rendered from the live report or
    # from its sort_keys JSON; a bundle's .txt and .json must agree to the byte.
    for name, data in sorted(report["harnesses"].items()):
        if data.get("status") not in ("ok", "partial"):
            lines.append(name + ": " + data["status"] + " — " + str(data.get("error") or data.get("reason") or data.get("looked_in", "")))
            continue
        lines.append("%s: %s session(s); main %s tokens, subagents %s tokens" %
                     (name, data["sessions"], data["main"]["total"], data["subagent"]["total"]))
        for model, cost in sorted(data["reference_cost"].items()):
            lines.append("  %s: reference $%.4f–$%.4f (%s); %d unpriced tokens" %
                         (model, cost["minimum_usd"], cost["maximum_usd"], cost["status"], cost["unpriced_tokens"]))
        if data.get("reported_cost_records"):
            lines.append("  harness-reported cost $%.4f; may reflect reference prices rather than billing" % data["reported_cost_usd"])
        if data.get("compactions"):
            lines.append("  %d compactions; %d pre-compaction context tokens (not tokens discarded)" % (data["compactions"], data["precompact_tokens"]))
        if data.get("diagnostics"):
            lines.append("  gaps: " + json.dumps(data["diagnostics"], sort_keys=True))
        if data.get("coverage"):
            lines.append("  coverage: " + data["coverage"])
    routing = report["routing"]
    lines += ["", "Claude dispatch requests: " + json.dumps(routing["tiers"], sort_keys=True),
              "Unspecified model does not prove inheritance; native profiles and overrides may choose it."]
    guard = report["guard"]
    if guard.get("error"):
        lines.append("Guard log unreadable: " + guard["error"])
    else:
        lines.append("Guard: %d retained records, %d blocked, %d confirmed executions in this window." %
                     (guard.get("records", 0), guard.get("blocked", 0), guard.get("confirmed_executions", 0)))
        lines.append("Rotated logs can omit older events; no records does not establish installation or hook failure.")
        if guard.get("errors"):
            lines.append("Guard errors: %d" % guard["errors"])
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
