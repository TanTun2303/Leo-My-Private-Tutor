"""Postgres connection pool with pgvector registered on every connection."""

from __future__ import annotations

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)
    conn.row_factory = dict_row  # type: ignore[assignment]


def make_pool(url: str, min_size: int = 1, max_size: int = 5) -> ConnectionPool:
    pool = ConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        configure=_configure,
        kwargs={"autocommit": False},
        open=False,
    )
    pool.open(wait=True, timeout=30)
    return pool
