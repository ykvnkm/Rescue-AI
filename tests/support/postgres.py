from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config as AlembicConfig

from libs.infra.postgres import (
    PostgresDatabase,
    dsn_with_search_path,
    resolve_postgres_dsn,
)

ROOT = Path(__file__).resolve().parents[2]


def resolve_test_postgres_dsn() -> str | None:
    explicit_test_dsn = os.getenv("APP_TEST_POSTGRES_DSN")
    if explicit_test_dsn:
        return explicit_test_dsn

    try:
        return resolve_postgres_dsn()
    except ValueError:
        return None


@contextmanager
def migrated_postgres_database(base_dsn: str) -> Iterator[PostgresDatabase]:
    schema = f"pytest_{uuid4().hex}"
    admin_db = PostgresDatabase(base_dsn)
    _create_schema(admin_db=admin_db, schema=schema)
    schema_dsn = dsn_with_search_path(base_dsn, schema)
    _upgrade_head(schema_dsn)
    try:
        yield PostgresDatabase(schema_dsn)
    finally:
        _drop_schema(admin_db=admin_db, schema=schema)


def _upgrade_head(dsn: str) -> None:
    alembic_config = AlembicConfig(str(ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(ROOT / "db_migrations"))
    previous_dsn = os.environ.get("APP_POSTGRES_DSN")
    os.environ["APP_POSTGRES_DSN"] = dsn
    try:
        command.upgrade(alembic_config, "head")
    finally:
        if previous_dsn is None:
            os.environ.pop("APP_POSTGRES_DSN", None)
        else:
            os.environ["APP_POSTGRES_DSN"] = previous_dsn


def _create_schema(*, admin_db: PostgresDatabase, schema: str) -> None:
    with admin_db.connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
        conn.commit()


def _drop_schema(*, admin_db: PostgresDatabase, schema: str) -> None:
    with admin_db.connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()