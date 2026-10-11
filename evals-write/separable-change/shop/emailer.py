"""Emailer calls."""
from shop import settings


def attempts():
    """How many times send() tries before it gives up."""
    return settings.MAX_RETRIES


def send(call):
    """Send a transactional email."""
    last = None
    for _ in range(attempts()):
        try:
            return call()
        except ConnectionError as error:
            last = error
    raise last
