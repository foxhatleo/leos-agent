"""Http client calls."""
from shop import settings


def attempts():
    """How many times fetch() tries before it gives up."""
    return settings.MAX_RETRIES


def fetch(call):
    """Fetch a URL, retrying on connection errors."""
    last = None
    for _ in range(attempts()):
        try:
            return call()
        except ConnectionError as error:
            last = error
    raise last
