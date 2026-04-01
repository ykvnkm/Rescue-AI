"""Initialize the remote Postgres schema (e.g. Supabase).

Usage:
    python -m rescue_ai.interfaces.cli.init_remote_db

Reads DB_DSN from env / .env and executes
infra/postgres/init/010-app-schema.sql against it.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

from rescue_ai.config import get_settings

_SQL_FILE = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "postgres"
    / "init"
    / "010-app-schema.sql"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    dsn = settings.database.dsn
    if not dsn:
        raise RuntimeError(
            "DB_DSN is required. Set it in .env or as an environment variable."
        )
    psycopg = importlib.import_module("psycopg")
    sql = _SQL_FILE.read_text(encoding="utf-8")

    logger.info("Connecting to remote Postgres...")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS app")
            cur.execute("SET search_path TO app")
            cur.execute(sql)
        conn.commit()

    logger.info("Remote schema initialized successfully ✓")


if __name__ == "__main__":
    main()
