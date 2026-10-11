"""Inventory calls."""
from shop import settings


def attempts():
    """How many times reserve() tries before it gives up."""
    return settings.MAX_RETRIES


def reserve(call):
    """Reserve stock for an order line."""
    last = None
    for _ in range(attempts()):
        try:
            return call()
        except ConnectionError as error:
            last = error
    raise last
