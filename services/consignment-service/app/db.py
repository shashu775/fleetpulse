"""Database connection pooling.

A pool reuses connections instead of opening a new one per request. This
matters on db.t3.micro, which allows only ~85 connections in total: with
max_size=5 and two services we use at most 10.
"""

import logging
import os
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

log = logging.getLogger("db")

_pool: ConnectionPool | None = None


def init_pool() -> None:
    """Open the connection pool. Called once at application startup."""
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
        # Wait up to 30s for Postgres on first boot rather than crash-looping.
        timeout=30,
        open=True,
    )
    _pool.wait(timeout=30)
    log.info("database pool ready")


def close_pool() -> None:
    """Close the pool on shutdown so connections are released cleanly."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_conn():
    """Borrow a connection from the pool.

    Usage:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(...)
            conn.commit()
    """
    if _pool is None:
        init_pool()
    assert _pool is not None
    with _pool.connection() as conn:
        yield conn
