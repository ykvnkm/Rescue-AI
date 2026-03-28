"""Initialize the remote Postgres schema (e.g. Supabase).

Usage:
    python -m rescue_ai.interfaces.cli.init_remote_db

Reads SYNC_REMOTE_POSTGRES_DSN from env / .env and executes
infra/postgres/init-remote/001-remote-schema.sql against it.
"""

from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path

from rescue_ai.config import get_settings

_SQL_FILE = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "postgres"
    / "init-remote"
    / "001-remote-schema.sql"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    dsn = settings.sync.remote_postgres_dsn
    if not dsn:
        dsn = os.getenv("SYNC_REMOTE_POSTGRES_DSN", "")
    if not dsn:
        raise RuntimeError(
            "SYNC_REMOTE_POSTGRES_DSN is required. "
            "Set it in .env or as an environment variable."
        )

    psycopg = importlib.import_module("psycopg")
    sql = _SQL_FILE.read_text(encoding="utf-8")

    logger.info("Connecting to remote Postgres...")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()

    logger.info("Remote schema initialized successfully ✓")


if __name__ == "__main__":
    main()
