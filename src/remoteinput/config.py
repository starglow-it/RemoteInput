import logging
import os
import ssl
from urllib.parse import urlsplit

try:
    from ._deployment import RELAY_URL
except ImportError:
    RELAY_URL = ""

QUEUE_LIMIT = 256
MAX_QUEUE_AGE = 0.100
LEASE_SECONDS = 0.850
CHALLENGE_INTERVAL = 0.200
SEND_TIMEOUT = 0.350
MAX_FRAME = 2048


def relay_url(override=None):
    url = override or os.environ.get("REMOTEINPUT_RELAY") or RELAY_URL
    parts = urlsplit(url)
    if parts.scheme != "wss" or not parts.hostname or parts.username or parts.password:
        raise ValueError("The developer must embed a deployed wss:// relay address; see docs/DEPLOY.md.")
    if parts.query or parts.fragment or parts.path != "/ws" or parts.hostname.endswith(".invalid"):
        raise ValueError("Relay address must be wss://your-host/ws with no query, fragment, or credentials.")
    return url


def client_options(ssl_context=None):
    return dict(ssl=ssl_context or ssl.create_default_context(), compression=None,
                max_size=MAX_FRAME, max_queue=16, write_limit=4096,
                open_timeout=10, close_timeout=1, ping_interval=10, ping_timeout=5, proxy=None)


def private_logging():
    # websocket DEBUG logging includes frame payloads. Never enable it in this application.
    for name in ("websockets", "websockets.client", "websockets.server"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)

