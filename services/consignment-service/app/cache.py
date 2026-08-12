"""Redis helpers.

Every function here FAILS SOFT. If Redis is down the application must keep
working, just slower -- a cache that can take down your service is worse than
having no cache at all. Read failures behave exactly like a cache miss.
"""

import json
import logging
import os

import redis

log = logging.getLogger("cache")

# from_url() does NOT connect eagerly, so importing this module is safe even
# when Redis is unreachable (which is what lets the unit tests run with no
# infrastructure at all).
_r = redis.from_url(
    os.getenv("REDIS_URL", "redis://redis:6379/0"),
    decode_responses=True,
    socket_timeout=2,
    socket_connect_timeout=2,
)


def cache_get(key: str) -> dict | None:
    try:
        raw = _r.get(key)
        return json.loads(raw) if raw else None
    except Exception as e:
        log.warning("cache read failed, continuing without cache: %s", e)
        return None


def cache_set(key: str, value: dict, ttl: int) -> None:
    try:
        _r.setex(key, ttl, json.dumps(value, default=str))
    except Exception as e:
        log.warning("cache write failed: %s", e)


def cache_delete(key: str) -> None:
    """Invalidate rather than update.

    If we wrote the new value and the surrounding transaction later rolled
    back, the cache would hold data that was never committed. Deleting is
    always safe -- the next read just re-fetches from Postgres.
    """
    try:
        _r.delete(key)
    except Exception as e:
        log.warning("cache delete failed: %s", e)
