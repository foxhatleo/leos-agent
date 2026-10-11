"""Search calls."""
from shop import settings


def attempts():
    """How many times query() tries before it gives up."""
    return settings.MAX_RETRIES


def query(call):
    """Run a search against the index."""
    last = None
    for _ in range(attempts()):
        try:
            return call()
        except ConnectionError as error:
            last = error
    raise last
