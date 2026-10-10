#!/usr/bin/env python3
"""Offline OpenRouter reference prices. Refresh uses HTTP, never a model.

Names are resolved for accounting only: callers must preserve the original
harness model identifier. Unknown and crossover prices do not enforce a ceiling.
"""
import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse

from state import _data_root, atomic_write

SOURCE = "https://openrouter.ai/api/v1/models"
BUNDLED = Path(__file__).resolve().parents[1] / "payload" / "model-prices.json"
FAMILIES = ("claude", "gpt", "deepseek", "kimi", "glm", "qwen")
MAX_BYTES = 8 * 1024 * 1024
TTL = 86400
# The widest version gap an estimate may bridge: one major step, or any
# distance within the same major version up to that.
NEARBY_VERSION_GAP = Decimal(1)
# Keys of a conditional rate row that are prices. Every other key is a
# condition. Only a prompt-size threshold has an ordering the comparison can
# evaluate point by point; any other condition falls back to rate ranges.
PRICE_KEYS = frozenset(("prompt", "completion", "input_cache_read", "input_cache_write", "input_cache_write_1h",
                        "web_search", "image", "image_output", "audio", "audio_output", "request",
                        "internal_reasoning"))
THRESHOLD = "min_prompt_tokens"
# Claude on Amazon Bedrock is `anthropic.` plus the first-party name and a
# `-v<n>[:<n>]` revision, optionally behind a cross-region inference prefix;
# the prefixes are the ones Claude Code itself recognizes. Vertex AI appends
# `@<date>` instead, and names Sonnet 3.5's second release `-v2@<date>`.
BEDROCK_CLAUDE = re.compile(r"^(?:(?:us|eu|apac|jp|au|us-gov|global)\.)?anthropic\."
                            r"(claude-[a-z0-9.-]+?)(?:-v\d+(?::\d+)?)?$")
VERTEX_CLAUDE = re.compile(r"^(claude-[a-z0-9.-]+?)(?:-v\d+)?@\d{8}$")


class PricingError(ValueError):
    pass


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def identity(name):
    """Provider-independent family, preserved variant, and numeric version.

Only recognized family grammar is normalized. No edit-distance matching across
variants, sizes, paid/free endpoints, or unknown provider prefixes. Claude's
Bedrock and Vertex IDs share the identity of the first-party ID.
"""
    if not isinstance(name, str):
        return None
    value = name.lower().strip().lstrip("~")
    if "/" in value:
        provider, value = value.rsplit("/", 1)
        if provider not in ("anthropic", "openai", "deepseek", "moonshotai", "moonshot",
                            "z-ai", "zhipu", "zhipuai", "qwen", "alibaba", "openrouter/anthropic",
                            "openrouter/openai", "openrouter/deepseek", "openrouter/moonshotai",
                            "openrouter/z-ai", "openrouter/qwen"):
            return None
    value = re.sub(r"\[(?:1m|200k)\]$", "", value)
    provider_id = BEDROCK_CLAUDE.match(value) or VERTEX_CLAUDE.match(value)
    if provider_id:
        value = provider_id.group(1)
    value = value.replace("_", "-").replace(" ", "-")
    value = re.sub(r"-(?:\d{8}|\d{4}|latest)$", "", value)
    if value in ("haiku", "sonnet", "opus", "fable"):
        value = "claude-" + value
    elif re.match(r"^(?:haiku|sonnet|opus|fable)-", value):
        value = "claude-" + value
    elif re.match(r"^(?:sol|terra|luna|astra)(?:-|$)", value):
        value = "gpt-" + value
    family = next((f for f in FAMILIES if value.startswith(f)), None)
    if family is None:
        return None
    tail = value[len(family):].strip("-")
    # Claude's public dated IDs use 4-5 where catalog IDs use 4.5.
    tail = re.sub(r"(?<!\d)(\d+)-(\d+)(?=-|$)", r"\1.\2", tail, count=1)
    match = re.search(r"(?<![a-z0-9])(?:v|k)?(\d+(?:\.\d+)?)(?![a-z0-9])", tail)
    version = tuple(int(p) for p in match.group(1).split(".")) if match else ()
    variant = (tail[:match.start()] + tail[match.end():]).strip("-") if match else tail
    variant = re.sub(r"-+", "-", variant)
    return family, variant, version


def _version_number(version):
    """(6, 5) -> 6.5, (6,) -> 6: the version as written, for distances."""
    return Decimal(".".join(str(part) for part in version)) if version else Decimal(0)


def snapshot(raw, fetched_at=None):
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
        raise PricingError("catalog must contain a data array")
    models = []
    seen = set()
    for row in raw["data"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise PricingError("invalid model entry")
        if identity(row["id"]) is None:
            continue
        if row["id"] in seen:
            raise PricingError("duplicate model entry: " + row["id"])
        seen.add(row["id"])
        prices = row.get("pricing")
        if not isinstance(prices, dict):
            raise PricingError("missing pricing for " + row["id"])
        for key, value in prices.items():
            if key != "overrides" and decimal(value) is None:
                raise PricingError("invalid price for " + row["id"])
        overrides = prices.get("overrides", [])
        if not isinstance(overrides, list) or any(not isinstance(v, dict) for v in overrides):
            raise PricingError("invalid conditional rates for " + row["id"])
        for override in overrides:
            for key in ("prompt", "completion", "input_cache_read", "input_cache_write"):
                if key in override and decimal(override[key]) is None:
                    raise PricingError("invalid conditional price for " + row["id"])
        models.append({k: row[k] for k in ("id", "canonical_slug", "name", "created", "pricing") if k in row})
    if not models:
        raise PricingError("catalog contains no supported model families")
    return {"schema": 1, "source": SOURCE, "fetched_at": fetched_at or time.time(), "models": models}


def cache_path():
    return Path(_data_root()) / "model-prices.json"


def load():
    """Read without creating files. Corrupt caches fall back to the snapshot."""
    for path in (cache_path(), BUNDLED):
        try:
            data = json.loads(path.read_text())
            if data.get("schema") == 1 and isinstance(data.get("models"), list):
                snapshot({"data": data["models"]})
                return data
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return {"schema": 1, "source": SOURCE, "fetched_at": None, "models": []}


@dataclass(frozen=True)
class Match:
    requested: str
    status: str
    model: object = None

    @property
    def pricing(self):
        return self.model.get("pricing", {}) if self.model else {}

    def report(self):
        return {"requested": self.requested, "status": self.status,
                "reference_model": self.model["id"] if self.model else None}


def resolve(name, catalog=None):
    rows = (catalog if catalog is not None else load())["models"]
    exact = [r for r in rows if name == r["id"] or name == r.get("canonical_slug")]
    if len(exact) == 1:
        return Match(name, "exact", exact[0])
    key = identity(name)
    if key is None:
        return Match(name, "unknown")
    candidates = [r for r in rows if identity(r["id"]) == key]
    # A plain model and several dated revisions can normalize identically.
    # Prefer the undated public endpoint; never pick between differing prices
    # arbitrarily when only dated endpoints exist.
    plain = [r for r in candidates if not re.search(r"-(?:\d{4}|\d{8}|latest)$", r["id"]) and not r["id"].startswith("~")]
    if len(plain) == 1:
        return Match(name, "alias", plain[0])
    if len(candidates) == 1:
        return Match(name, "alias", candidates[0])
    if candidates:
        return Match(name, "unknown")
    family, variant, version = key
    nearby = []
    current = _version_number(version)
    for row in rows:
        item = identity(row["id"])
        if not item or item[:2] != (family, variant) or not item[2] or row["id"].startswith("~"):
            continue
        other = _version_number(item[2])
        if version:
            # One numeric gap for every family: 6.5 sits 0.5 from 6 and 0.9
            # from 5.6. An equal gap prefers the same major version, so 5 is
            # estimated from 5.5 rather than from 4.5.
            gap = abs(other - current)
            if gap > NEARBY_VERSION_GAP:
                continue
            distance = (gap, item[2][0] != version[0])
        else:
            distance = (-other, False)
        nearby.append((distance, row))
    if not nearby:
        return Match(name, "unknown")
    best = min(distance for distance, _ in nearby)
    winners = [row for distance, row in nearby if distance == best]
    if len(winners) != 1:
        return Match(name, "unknown")
    return Match(name, "estimated" if version else "alias", winners[0])


def _steps(match):
    """Sorted (threshold, rate row) prompt-size tiers, or None for any other condition."""
    steps = []
    for override in match.pricing.get("overrides", []):
        conditions = {k: v for k, v in override.items() if k not in PRICE_KEYS}
        threshold = conditions.get(THRESHOLD)
        if set(conditions) != {THRESHOLD} or isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
            return None
        steps.append((threshold, override))
    if len({threshold for threshold, _ in steps}) != len(steps):
        return None
    return sorted(steps, key=lambda step: step[0])


def _rates_at(match, steps, tokens):
    """Prompt and completion rates for a request of `tokens` prompt tokens."""
    active = match.pricing
    for threshold, override in steps:
        if threshold <= tokens:
            active = override
    return [decimal(active.get(key, match.pricing.get(key))) for key in ("prompt", "completion")]


def compare(child, parent, catalog=None):
    """Return allowed/over-ceiling/unknown with auditable reference matches.

Prompt-size tiers are compared at every threshold either model has, with both
models priced for the same request. A child priced at or above the parent at
the base rate is over the ceiling even when a long-prompt tier crosses over:
the base rate is what ordinary dispatches pay. Other conditional rates are
compared as ranges, so only unambiguous dominance enforces the ceiling.
Search/image charges are reported by accounting, not assumed for every text
request. Fixed request/reasoning fees make this comparison unknown.
"""
    catalog = catalog if catalog is not None else load()
    c, p = resolve(child, catalog), resolve(parent, catalog)
    report = {"child": c.report(), "parent": p.report(), "source": SOURCE,
              "fetched_at": catalog.get("fetched_at"), "status": "unknown"}
    if c.model is None or p.model is None:
        return report
    if c.model["id"] == p.model["id"]:
        report["status"] = "allowed"
        return report

    def ranges(match):
        prices = match.pricing
        if any(decimal(prices.get(k, 0)) != 0 for k in ("request", "internal_reasoning")):
            return None
        overrides = prices.get("overrides", [])
        if not isinstance(overrides, list) or any(not isinstance(v, dict) for v in overrides):
            return None
        out = []
        for key in ("prompt", "completion"):
            values = [decimal(prices.get(key))] + [decimal(v.get(key, prices.get(key))) for v in overrides]
            if None in values:
                return None
            out.append((min(values), max(values)))
        return out

    cr, pr = ranges(c), ranges(p)
    if cr is None or pr is None:
        return report
    cs, ps = _steps(c), _steps(p)
    if cs is not None and ps is not None:
        points = sorted({0} | {t for t, _ in cs} | {t for t, _ in ps})
        rates = [pair for point in points
                 for pair in zip(_rates_at(c, cs, point), _rates_at(p, ps, point))]
        if any(None in pair for pair in rates):
            return report
        base = rates[:2]
        if all(cv <= pv for cv, pv in rates):
            report["status"] = "allowed"
        elif all(cv >= pv for cv, pv in rates) and any(cv > pv for cv, pv in rates):
            report["status"] = "over-ceiling"
        elif all(cv >= pv for cv, pv in base) and any(cv > pv for cv, pv in base):
            report["status"] = "over-ceiling"
            report["basis"] = "base-rate"
        return report

    def schedule(match):
        return [json.dumps({k: v for k, v in override.items() if k not in PRICE_KEYS}, sort_keys=True)
                for override in match.pricing.get("overrides", [])]

    if schedule(c) == schedule(p):
        # When schedules agree, compare corresponding rates. Taking unrelated
        # min/max ranges would miss Terra > Sol under every matching condition.
        pairs = [(c.pricing, p.pricing)] + list(zip(c.pricing.get("overrides", []), p.pricing.get("overrides", [])))
        rates = [(decimal(cv.get(k, c.pricing[k])), decimal(pv.get(k, p.pricing[k])))
                 for cv, pv in pairs for k in ("prompt", "completion")]
        if all(cv <= pv for cv, pv in rates):
            report["status"] = "allowed"
        elif all(cv >= pv for cv, pv in rates) and any(cv > pv for cv, pv in rates):
            report["status"] = "over-ceiling"
        return report
    if all(cv[1] <= pv[0] for cv, pv in zip(cr, pr)):
        report["status"] = "allowed"
    elif all(cv[0] >= pv[1] for cv, pv in zip(cr, pr)) and any(cv[0] > pv[1] for cv, pv in zip(cr, pr)):
        report["status"] = "over-ceiling"
    return report


def refresh(force=False, opener=None):
    """Nonblocking lock, bounded HTTP, atomic replacement; retain old on error."""
    if opener is None:
        # Imported here: every dispatch loads this module, and only a refresh
        # needs an HTTP client.
        from urllib.request import urlopen as opener
    path = cache_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        marker = path.with_suffix(".attempt")
        if not force and marker.exists() and time.time() - marker.stat().st_mtime < TTL:
            return False
        marker.touch(mode=0o600)
        url, rows, seen = SOURCE, [], set()
        # Default API response is unpaginated. Still honor bounded pagination.
        for _ in range(10):
            if url in seen or urlparse(url).netloc != "openrouter.ai" or not url.startswith("https://"):
                raise PricingError("invalid catalog pagination")
            seen.add(url)
            with opener(url, timeout=5) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise PricingError("catalog response too large")
            page = json.loads(raw)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise PricingError("invalid catalog response")
            rows.extend(page["data"])
            next_url = (page.get("links") or {}).get("next")
            if not next_url:
                atomic_write(str(path), snapshot({"data": rows}))
                atomic_write(str(path.with_suffix(".status.json")), {"status": "ok", "attempted_at": time.time()})
                return True
            url = urljoin(SOURCE, next_url)
        raise PricingError("catalog exceeded pagination limit")
    except (OSError, ValueError, TypeError) as exc:
        try:
            atomic_write(str(path.with_suffix(".status.json")), {"status": "error", "attempted_at": time.time(), "error": str(exc)[:300]})
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


# Either set to "off" switches the session-start refresh off. `claude plugin
# eval` passes a run only an allowlist and EVAL_* variables, and each run has a
# fresh home with no catalog, so the eval cases need the prefixed spelling.
REFRESH_OPT_OUT = ("LEOS_AGENT_PRICE_REFRESH", "EVAL_LEOS_AGENT_PRICE_REFRESH")


def refresh_disabled(environ=None):
    environ = os.environ if environ is None else environ
    return any(environ.get(name) == "off" for name in REFRESH_OPT_OUT)


def refresh_background(force=False):
    path = cache_path().with_suffix(".attempt")
    try:
        if not force and path.exists() and time.time() - path.stat().st_mtime < TTL:
            return
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "refresh"] + (["--force"] if force else []),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("refresh", "show", "resolve"))
    parser.add_argument("model", nargs="?")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "refresh":
            print("refreshed" if refresh(args.force) else "fresh or refresh already running")
        elif args.command == "resolve":
            print(json.dumps(resolve(args.model).report(), indent=2))
        else:
            print(json.dumps(load(), indent=2))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"pricing: refresh failed; keeping last known catalog: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
