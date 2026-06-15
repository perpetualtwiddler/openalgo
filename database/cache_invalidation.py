# database/cache_invalidation.py
"""
ZeroMQ-based cache invalidation for cross-process delivery.

The websocket proxy (port 8765) holds local in-memory caches that need
to be cleared when the Flask worker re-issues auth tokens or revokes a
session. These caches include `feed_token_cache`, `auth_cache`,
`verified_api_key_cache`, `invalid_api_key_cache`, and `broker_cache`
in `websocket_proxy/server.py`. Without invalidation, the proxy keeps
serving stale tokens after a re-login and clients see HTTP 403 errors
(GitHub issue #765).

Architecture — DEDICATED CONTROL CHANNEL (#1499)
================================================
This module owns its own `zmq.PUB` socket bound on `CACHE_INV_PORT`
(default 5557). The websocket proxy's SUB socket connects to *both*
`ZMQ_PORT` (market data) and `CACHE_INV_PORT` (control plane) and
routes the latter by topic prefix `CACHE_INVALIDATE_*` to
`_handle_cache_invalidation` in server.py.

Fix history
-----------
* **issue #765** — proxy held stale caches after re-login. Originally
  fixed by adding a ZMQ pub/sub control channel here.
* **issue #1374** — the original control channel was broken: this
  module created a PUB socket and `connect()`-ed to the same endpoint
  the market-data PUB `bind()`-s. Two PUBs on one wire is invalid ZMQ
  topology; messages were silently dropped. The fix at the time
  delegated publishing through the market-data `SharedZmqPublisher`
  singleton — i.e. piggy-backed on the market-data bus. That was safe
  while gunicorn ran proxy + worker in the same process.
* **issue #1421** — split the WS proxy into its own subprocess. After
  this, the singleton from #1374 stopped being a singleton across
  processes: worker and proxy each instantiated their own
  `SharedZmqPublisher`, the worker won the race to bind :5555, and the
  proxy fell back to :5556 silently. The proxy's SUB only listened to
  :5555 → market data was dropped. Strategies saw `Feed STALE` despite
  a healthy broker feed (2026-06-08 incident).
* **current (#1499)** — restore the dedicated-control-channel topology
  the audit at `docs/audit/websocket-broker-priority.md` already
  recommends: PUB binds its own port here, proxy SUB-connects to it.
  No singleton race, no overloaded shared bus.
"""

import json
import os
import threading

import zmq

from utils.logging import get_logger

logger = get_logger(__name__)

# Cache invalidation message types
CACHE_INVALIDATION_PREFIX = "CACHE_INVALIDATE"
AUTH_CACHE_TYPE = "AUTH"
FEED_CACHE_TYPE = "FEED"
ALL_CACHE_TYPE = "ALL"

# Singleton publisher instance
_publisher_instance = None
_publisher_lock = threading.Lock()


class CacheInvalidationPublisher:
    """ZMQ PUB owner for cache-invalidation control messages.

    Binds once, lazily, on the first publish call. Bind failures are
    raised loudly (no port-scanning fallback) — silent fallback was the
    root cause of the 2026-06-08 stale-feed incident.
    """

    def __init__(self) -> None:
        self._socket = None
        self._context = None
        self._bound_endpoint = None
        self._lock = threading.Lock()

    def _ensure_bound(self) -> None:
        if self._socket is not None:
            return
        with self._lock:
            if self._socket is not None:
                return
            host = os.getenv("ZMQ_HOST", "127.0.0.1")
            port = int(os.getenv("CACHE_INV_PORT", "5557"))
            endpoint = f"tcp://{host}:{port}"

            ctx = zmq.Context.instance()
            sock = ctx.socket(zmq.PUB)
            sock.setsockopt(zmq.LINGER, 1000)
            sock.setsockopt(zmq.SNDHWM, 1000)
            # Bind explicitly; let zmq.ZMQError propagate if the port is
            # taken. We do NOT scan forward — silent fallback hid the
            # #1421 race for weeks. A loud crash at startup is the
            # correct failure mode here.
            sock.bind(endpoint)

            self._context = ctx
            self._socket = sock
            self._bound_endpoint = endpoint
            logger.info(f"Cache-invalidation PUB bound to {endpoint}")

    def publish_invalidation(self, user_id: str, cache_type: str = ALL_CACHE_TYPE) -> bool:
        """Publish a cache invalidation message for a specific user.

        Args:
            user_id: The user whose cache should be invalidated
            cache_type: Type of cache to invalidate (AUTH, FEED, or ALL)
        """
        if not user_id:
            logger.warning("Cache invalidation skipped — no user_id supplied")
            return False

        try:
            self._ensure_bound()
            topic = f"{CACHE_INVALIDATION_PREFIX}_{cache_type}_{user_id}"
            message = {
                "action": "invalidate",
                "user_id": user_id,
                "cache_type": cache_type,
            }
            self._socket.send_multipart(
                [topic.encode("utf-8"), json.dumps(message).encode("utf-8")]
            )
            logger.info(f"Published cache invalidation for user: {user_id}, type: {cache_type}")
            return True
        except Exception as e:
            logger.exception(f"Failed to publish cache invalidation for user {user_id}: {e}")
            return False

    def close(self) -> None:
        """Close the PUB socket. Safe to call multiple times."""
        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.close(linger=1000)
                except Exception as e:
                    logger.warning(f"Error closing cache-invalidation socket: {e}")
                finally:
                    self._socket = None
                    self._bound_endpoint = None


def get_cache_invalidation_publisher() -> CacheInvalidationPublisher:
    """Return the singleton cache invalidation publisher."""
    global _publisher_instance

    if _publisher_instance is None:
        with _publisher_lock:
            if _publisher_instance is None:
                _publisher_instance = CacheInvalidationPublisher()

    return _publisher_instance


def publish_auth_cache_invalidation(user_id: str) -> bool:
    """Convenience function to publish an AUTH-cache invalidation."""
    return get_cache_invalidation_publisher().publish_invalidation(user_id, AUTH_CACHE_TYPE)


def publish_feed_cache_invalidation(user_id: str) -> bool:
    """Convenience function to publish a FEED-cache invalidation."""
    return get_cache_invalidation_publisher().publish_invalidation(user_id, FEED_CACHE_TYPE)


def publish_all_cache_invalidation(user_id: str) -> bool:
    """Convenience function to publish an ALL-cache invalidation."""
    return get_cache_invalidation_publisher().publish_invalidation(user_id, ALL_CACHE_TYPE)
