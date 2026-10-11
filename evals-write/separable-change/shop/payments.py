"""Payments calls."""
from shop import settings


def attempts():
    """How many times charge() tries before it gives up."""
    return getattr(settings, "MAX_RETRIES")


def charge(call):
    """Charge a card through the payment provider."""
    last = None
    for _ in range(attempts()):
        try:
            return call()
        except ConnectionError as error:
            last = error
    raise last
