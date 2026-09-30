"""Apply SQL migrations in order, once, under a Postgres advisory lock."""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)
LOCK_KEY = 0x1E0_3E3  # arbitrary, stable


def migrate(url: str, migrations_dir: Path) -> list[str]:
    """Apply pending migrations. Returns the versions applied in this call."""
    applied_now: list[str] = []
    files = sorted(migrations_dir.glob("*.sql"))
    # Plain connection (no vector registration: the extension may not exist yet).
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            exists = conn.execute("SELECT to_regclass('memory.schema_migrations') IS NOT NULL").fetchone()
            done: set[str] = set()
            if exists and exists[0]:
                done = {r[0] for r in conn.execute("SELECT version FROM memory.schema_migrations")}
            for f in files:
                version = f.stem
                if version in done:
                    continue
                with conn.transaction():
                    conn.execute(f.read_text())  # type: ignore[arg-type]
                    conn.execute("INSERT INTO memory.schema_migrations (version) VALUES (%s)", (version,))
                log.info("applied migration %s", version)
                applied_now.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return applied_now


if __name__ == "__main__":
    from .settings import get_settings

    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    print(migrate(s.database_url, s.leo_migrations_dir))
