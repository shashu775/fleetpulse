"""Redis access for dispatch-service.

NOTE the difference from consignment-service's cache.py, which fails soft.

For consignment, Redis is a CACHE -- if it is down, fall back to Postgres and
carry on slower. Here Redis is the STORE: last-known vehicle position lives
nowhere else. There is nothing to fall back to, so failures propagate and the
GPS endpoints return 503 rather than silently pretending to have stored a
position they discarded.

Knowing which of the two a given Redis is for you is the whole distinction.
"""

import os

import redis

# from_url() is lazy -- no connection is attempted at import time, which keeps
# the unit tests runnable with no infrastructure.
_client = redis.from_url(
    os.getenv("REDIS_URL", "redis://redis:6379/0"),
    decode_responses=True,
    socket_timeout=2,
    socket_connect_timeout=2,
)


def redis_client() -> redis.Redis:
    return _client
