"""Retry backoff helpers."""


def retry_delays(limit, base_ms=100, cap_ms=5000):
    """Return `limit` delays in milliseconds: base_ms, doubling each time, each capped at cap_ms."""
    raise NotImplementedError
