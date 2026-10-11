#!/usr/bin/env python3
"""statusline: an opt-in Claude Code status line for leos-agent.

Claude Code runs this with its status-line JSON on stdin (model, session_id,
cost.total_cost_usd, ...) and shows the one line it prints. The line names the
current model, Claude Code's own session cost estimate when stdin carries one,
and, from the dispatch log, this session's children by tier with their
reference cost. Counting and pricing come from dispatch_log.summarise, the same
code `dispatch_log.py report` uses, so the two never disagree about a child.

It reads local files only: no network, no model call, no writes. Any failure
prints the plainest line it can rather than an error, since a status line has
no one to report to. Installed only on request: leo-install.py claude --statusline.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Tiers in capability order; anything else (unrouted, ambiguous) follows by name.
TIER_ORDER = ("cheap", "standard", "premium", "parent")
SEPARATOR = " | "


def _clean(value, limit=40):
    """Printable text only, bounded: the model name comes from outside."""
    return "".join(ch for ch in str(value) if ch.isprintable())[:limit]


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else None


def model_name(event):
    model = event.get("model") if isinstance(event, dict) else None
    if isinstance(model, dict):
        name = model.get("display_name") or model.get("id")
        if isinstance(name, str) and name.strip():
            return _clean(name.strip())
    return None


def _usd(low, high=None):
    high = low if high is None else high
    text = "$%.2f" % low
    return text if "$%.2f" % high == text else "%s-$%.2f" % (text, high)


def children(session_id, rows=None, catalog=None):
    """(dispatched per tier, reference (low, high) or None, unpriced children) for one Claude session."""
    import dispatch_log
    digest = dispatch_log.digest(session_id)
    if not digest:
        return {}, None, 0
    rows = dispatch_log.read() if rows is None else rows
    mine = [row for row in rows if row.get("session") == digest and row.get("harness") == "claude"]
    if not mine:
        return {}, None, 0
    summary = dispatch_log.summarise(mine, catalog)
    ran = {tier: bucket["ran"] for tier, bucket in summary["coverage"].items() if bucket.get("ran")}
    low = high = 0.0
    priced = unpriced = 0
    for bucket in summary["cost"].values():
        low += bucket["reference_usd"][0]
        high += bucket["reference_usd"][1]
        priced += bucket["priced_runs"]
        unpriced += bucket["unpriced_runs"]
    return ran, ((low, high) if priced else None), unpriced


def render(event, rows=None, catalog=None):
    parts = [model_name(event) or "Claude"]
    cost = event.get("cost") if isinstance(event.get("cost"), dict) else {}
    session_usd = _number(cost.get("total_cost_usd"))
    if session_usd is not None:
        parts.append("session " + _usd(session_usd))
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id:
        try:
            ran, reference, unpriced = children(session_id, rows, catalog)
        except Exception:
            ran, reference, unpriced = {}, None, 0  # the log is optional; the rest of the line still stands
        if ran:
            order = sorted(ran, key=lambda t: (TIER_ORDER.index(t) if t in TIER_ORDER else len(TIER_ORDER), t))
            text = "children " + " ".join("%s %d" % (tier, ran[tier]) for tier in order)
            if reference is not None:
                text += " ref " + _usd(*reference)
            if unpriced:
                text += " (%d unpriced)" % unpriced
            parts.append(text)
    return SEPARATOR.join(parts)


def main(stdin=None, stdout=None):
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    event = {}
    try:
        event = json.loads(stdin.read() or "{}")
        if not isinstance(event, dict):
            event = {}
        line = render(event)
    except Exception:
        line = model_name(event) or "Claude"
    stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
