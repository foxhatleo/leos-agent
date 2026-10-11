"""outcome: the fixed tokens a worker's reply and an escalating brief carry.

Pure and import-free on purpose. Both the dispatch hot path and the log reader
call into here, and neither may pay for I/O or pull in the rest of the tree.

Everything returned is an enum member, a tri-state boolean, or an integer. The
evidence behind a `Verified:` line and the failure text after an escalation
marker are read to classify and then dropped: the dispatch log promises to hold
no brief or result text, and that promise is kept structurally here rather than
by each caller remembering to strip a field.
"""
import re

OUTCOMES = ("done", "partial", "blocked", "escalate")
TIERS = ("cheap", "standard", "premium")
TAIL_BYTES = 4096
HEAD_BYTES = 512
HEAD_LINES = 5
TAIL_LINES = 40

_WRAP = "*_`~ \t"
# The token must end the word: an echoed contract template such as
# `Result: done|partial|blocked|escalate` is not a `done`.
# A colon, hyphen, en or em dash, or equals sign separates the label from its
# value: `Result — done` is as common in model prose as `Result: done`.
_SEP = r"[*_`]*\s*[:=\-–—]"
_RESULT_RE = re.compile(r"^result" + _SEP + r"[\s*_`]*([a-z]+)(?=$|[\s.,;!)*_`~])", re.IGNORECASE)
_VERIFIED_RE = re.compile(r"^verified" + _SEP + r"(.*)$", re.IGNORECASE)
_ESCALATION_RE = re.compile(r"^escalation\s+from\s+(cheap|standard|premium)\b[*_`]*\s*:", re.IGNORECASE)
# Decided from the first word, so `none (read-only task)`, `None - no tests
# exist` and `not run` all read as no evidence rather than as stated evidence.
_NO_EVIDENCE = frozenset(("", "none", "n/a", "na", "no", "nothing", "not", "unverified", "untested", "skipped"))
_FIRST_WORD_RE = re.compile(r"[^\s(\[,;:.!?\u2013\u2014-]*")


def _normalise(line):
    """Strip markdown furniture so `**Result:** done` and `> Result: done` both match."""
    text = line.strip()
    while text and text[0] in ">#-":
        text = text[1:].lstrip()
    return text.strip(_WRAP)


def _unwrap(value):
    return value.strip().strip(_WRAP).rstrip(".").strip().casefold()


def _evidence(value):
    """True for stated evidence, False for none, None for an unfilled `<placeholder>`."""
    text = _unwrap(value)
    if text.startswith("<"):
        return None
    return _FIRST_WORD_RE.match(text).group(0) not in _NO_EVIDENCE


def parse(text):
    """Classify a worker's final reply.

    Scans the last TAIL_LINES non-empty lines from the bottom; the first match
    from the bottom wins, so a reply that quotes the contract earlier on cannot
    outvote its own closing line. A `Verified:` with nothing after it takes its
    evidence from the next non-empty line, unless that line is another contract
    line. Unknown tokens are `unknown`, never echoed.
    """
    result = {"outcome": "unknown", "verified": None}
    if not isinstance(text, str) or not text:
        return result
    lines = [_normalise(ln) for ln in text[-TAIL_BYTES:].splitlines() if ln.strip()][-TAIL_LINES:]
    found_result = found_verified = False
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if not found_result:
            match = _RESULT_RE.match(line)
            if match:
                found_result = True
                token = match.group(1).casefold()
                result["outcome"] = token if token in OUTCOMES else "unknown"
                continue
        if not found_verified:
            match = _VERIFIED_RE.match(line)
            value = match.group(1) if match else None
            if value is not None and not _unwrap(value) and index + 1 < len(lines):
                following = lines[index + 1]
                if not (_RESULT_RE.match(following) or _VERIFIED_RE.match(following)):
                    value = following
            stated = _evidence(value) if value is not None else None
            if stated is not None:
                found_verified = True
                result["verified"] = stated
        if found_result and found_verified:
            break
    return result


def contract_met(parsed):
    """Whether a parse() result closes the worker contract: a known outcome and a stated evidence line.
    `Verified: none` states that there is none; an unfilled placeholder states nothing."""
    return parsed["outcome"] != "unknown" and parsed["verified"] is not None


def escalation_tier(head):
    """The tier an escalating brief names in its opening line, or None.

    Only the first HEAD_LINES non-empty lines of the first HEAD_BYTES are
    consulted: the marker is a header by contract, and a mention deeper in the
    brief is prose about escalation, not an escalation.
    """
    if not isinstance(head, str) or not head:
        return None
    lines = [ln for ln in head[:HEAD_BYTES].splitlines() if ln.strip()][:HEAD_LINES]
    for raw in lines:
        match = _ESCALATION_RE.match(_normalise(raw))
        if match:
            return match.group(1).casefold()
    return None
