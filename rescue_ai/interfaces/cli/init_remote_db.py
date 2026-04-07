"""Initialize the remote Postgres schema (e.g. Supabase).

Usage:
    python -m rescue_ai.interfaces.cli.init_remote_db

Reads DB_DSN from env / .env and executes all SQL files from
infra/postgres/init in lexicographical order.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

from rescue_ai.config import get_settings
from rescue_ai.infrastructure.postgres_connection import (
    _CONNECT_TIMEOUT_SEC,
    _ensure_compat_dsn,
)

_SQL_DIR = Path(__file__).resolve().parents[3] / "infra" / "postgres" / "init"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _iter_sql_files() -> list[Path]:
    """Return migration SQL files sorted by filename."""
    return sorted(path for path in _SQL_DIR.glob("*.sql") if path.is_file())


def main() -> None:
    settings = get_settings()
    dsn = settings.database.dsn
    if not dsn:
        raise RuntimeError(
            "DB_DSN is required. Set it in .env or as an environment variable."
        )
    psycopg = importlib.import_module("psycopg")
    sql_files = _iter_sql_files()
    if not sql_files:
        raise RuntimeError(f"No SQL migration files found in {_SQL_DIR}")

    logger.info("Connecting to remote Postgres...")
    with psycopg.connect(
        _ensure_compat_dsn(dsn),
        connect_timeout=_CONNECT_TIMEOUT_SEC,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS app")
            cur.execute("SET search_path TO app")
            for sql_file in sql_files:
                logger.info("Applying migration: %s", sql_file.name)
                cur.execute(sql_file.read_text(encoding="utf-8"))
        conn.commit()

    logger.info("Remote schema initialized successfully ✓")


if __name__ == "__main__":
    main()
