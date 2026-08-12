"""Database connection pooling for dispatch-service.

Identical to consignment-service's db.py. Duplicated rather than shared: two
services with one small helper each do not justify a shared library, and
copying it keeps each service independently deployable.
"""

import logging
import os
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

log = logging.getLogger("db")

_pool: ConnectionPool | None = None


def init_pool() -> None:
    global _pool
    if _pool is not None:
        return

    # A bare os.environ["..."] here raises KeyError with a 40-line traceback
    # and no hint about what to do. Say what is wrong and how to fix it.
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env, or run this "
            "service via `docker compose up` which supplies it."
        )

    _pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=5,
        timeout=30,
        open=True,
    )
    _pool.wait(timeout=30)
    log.info("database pool ready")


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_conn():
    if _pool is None:
        init_pool()
    assert _pool is not None
    with _pool.connection() as conn:
        yield conn
