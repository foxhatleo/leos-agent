"""Shipping calls."""
from shop import settings


def attempts():
    """How many times quote() tries before it gives up."""
    return settings.MAX_RETRIES


def quote(call):
    """Ask the carrier for a shipping quote."""
    last = None
    for _ in range(attempts()):
        try:
            return call()
        except ConnectionError as error:
            last = error
    raise last
