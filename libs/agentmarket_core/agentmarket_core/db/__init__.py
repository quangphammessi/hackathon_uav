"""PostgreSQL access: one shared connection pool per process, plus the
idempotent schema migration every service runs at startup.

A connection pool (rather than a connection per request) is the difference
between a service that holds a steady p99 under agent traffic and one that
spends it opening sockets -- proposal §7.3's "connection-pooled, no per-call
handshake" requirement.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from agentmarket_core.config import settings

log = logging.getLogger("agentmarket.db")

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_pool() -> ConnectionPool:
    """Process-wide lazily-created pool. Safe to call from any thread."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=settings.database_url,
                    min_size=1,
                    max_size=10,
                    kwargs={"row_factory": dict_row, "autocommit": True},
                    open=True,
                    timeout=10.0,
                )
    return _pool


def connection() -> Any:
    """Context manager yielding a pooled connection."""
    return get_pool().connection()


def query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            return list(cur.fetchall())


def query_one(sql: str, params: tuple | dict | None = None) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | dict | None = None) -> int:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def wait_for_postgres(timeout_seconds: float = 60.0) -> None:
    """Block until Postgres accepts a connection.

    Compose brings containers up in parallel; a service that assumes its
    database is already listening is the single most common reason a stack
    comes up "broken" on a cold start. `depends_on: condition: service_healthy`
    covers most of it, but this makes the service correct even without it.
    """
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001 -- any connection failure means "not ready yet"
            last_error = exc
            time.sleep(1.0)
    raise RuntimeError(f"Postgres not reachable within {timeout_seconds}s: {last_error}")


def migrate() -> None:
    """Apply schema.sql. Every statement is CREATE ... IF NOT EXISTS, so this
    is safe to run on every boot of every service, in any order."""
    sql = SCHEMA_PATH.read_text()
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(sql)
    log.info("schema applied (%s)", SCHEMA_PATH.name)


def reset_pool() -> None:
    """Test helper -- drops the cached pool so the next call re-reads config."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:  # noqa: BLE001
                pass
        _pool = None
