"""accounting: how observed tokens are counted and priced, in one place.

The usage scan (usage_scan.py) and the dispatch report (session_models.py for a
child's usage, dispatch_log.py for its reference cost) read the same Claude and
Codex transcripts and price the same tokens. Both import these rules, so the
two reports cannot disagree about one child.

Import-free on purpose: session_models sits on the dispatch guard's path and
dispatch_log on every hook that writes a row. Only the cost function loads the
price catalog module, and only its callers, which already need it, call it.
"""

TOKEN_KEYS = ("input", "cache_read", "cache_write", "output")
CLAUDE_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
CODEX_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
# The catalog rate that prices each of TOKEN_KEYS.
RATE_KEYS = ("prompt", "input_cache_read", "input_cache_write", "completion")

# Codex gaps, named as the usage scan reports them.
MISSING_TOTAL = "missing_cumulative_usage"
MISSING_BASELINE = "missing_initial_delta"
RESET = "cumulative_resets"


def count(value):
    """A token count: a non-negative integer, else 0. Booleans are not counts."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def claude_key(record, message, fallback):
    """The model request one Claude assistant record belongs to.

    Claude writes a request as several records, its content blocks and its
    streaming usage snapshots, under one requestId, or one message id when the
    record has none; `fallback` keys a record that has neither.
    """
    return record.get("requestId") or message.get("id") or fallback


def claude_usage(message):
    """A Claude assistant message's usage in TOKEN_KEYS order, or None when it reports none.
    Claude's cache categories are already separate from input."""
    usage = message.get("usage")
    return claude_values(usage) if isinstance(usage, dict) and usage else None


def claude_values(usage):
    return tuple(count(usage.get(key)) for key in CLAUDE_USAGE_KEYS)


def keep_largest(prior, values):
    """Merge one more snapshot of a request: snapshots are cumulative, so each category keeps its largest."""
    return tuple(max(a, b) for a, b in zip(prior, values)) if prior else tuple(values)


class CodexDeltas:
    """Per-request usage from one Codex rollout's token_count events, fed in file order.

    Totals are cumulative, so a request is the change since the previous total,
    and a repeated total is one event written again. A total that falls is a
    reset: the event's last_token_usage stands in for the request. With neither
    a baseline nor a last usage, the request cannot be established. Feed every
    event, inside a time window or not: earlier events establish the baseline.
    """

    def __init__(self):
        self.previous = None

    def step(self, payload):
        """(delta, gap) for one token_count payload.

        delta is the request's raw (input, cached, cache write, output) in
        CODEX_USAGE_KEYS order, or None when the event adds nothing; pass it to
        codex_request. gap is None or one of MISSING_TOTAL, MISSING_BASELINE
        and RESET. An event without usage info carries rate limits only, which
        Codex sends before the first response; nothing is missing there.
        """
        info = payload.get("info")
        if info is None:
            return None, None
        total = info.get("total_token_usage") if isinstance(info, dict) else None
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        if not isinstance(total, dict):
            return None, MISSING_TOTAL  # repeated last-only events cannot be deduplicated safely
        current = tuple(count(total.get(key)) for key in CODEX_USAGE_KEYS)
        previous, self.previous = self.previous, current
        if current == previous:
            return None, None
        if previous is not None and all(a >= b for a, b in zip(current, previous)):
            return tuple(a - b for a, b in zip(current, previous)), None
        if isinstance(last, dict):
            return tuple(count(last.get(key)) for key in CODEX_USAGE_KEYS), RESET if previous is not None else None
        return None, MISSING_BASELINE


def codex_request(delta):
    """A Codex delta in TOKEN_KEYS order. Codex input includes its cached input,
    which is taken out so the categories are disjoint; output already includes
    reasoning."""
    inp, read, write, output = delta
    return max(0, inp - read - write), read, write, output


def reference_range(match, amounts):
    """(low, high, unpriced) for token `amounts` keyed by TOKEN_KEYS, at one catalog match.

    Each category is priced at the base rate and at every conditional rate row,
    a row's missing rate falling back to the base, so conditional pricing gives
    a range. A category whose rate is missing or invalid in any row is unpriced:
    its tokens add to `unpriced` and nothing to the range. low and high are
    Decimal USD. A reference estimate from public catalog prices, never a bill.
    """
    import pricing
    from decimal import Decimal
    base = match.pricing
    rows = [base] + list(base.get("overrides", []))
    low, high, unpriced = Decimal(0), Decimal(0), 0
    for key, rate_key in zip(TOKEN_KEYS, RATE_KEYS):
        amount = count(amounts.get(key))
        if not amount:
            continue
        rates = [pricing.decimal(row.get(rate_key, base.get(rate_key))) for row in rows]
        if None in rates:
            unpriced += amount
            continue
        low += amount * min(rates)
        high += amount * max(rates)
    return low, high, unpriced
