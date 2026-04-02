"""Pytest configuration: Postgres fixtures and default env setup."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest


def _load_app_schema_sql() -> str:
    root = Path(__file__).resolve().parents[1]
    schema_path = root / "infra" / "postgres" / "init" / "010-app-schema.sql"
    return schema_path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[tuple[str, str]]:
    """Session-scoped Postgres DSN with an isolated test schema.

    Reads ``TEST_POSTGRES_DSN`` from environment.  If absent the fixture
    calls ``pytest.skip`` so integration tests are silently skipped when
    no database is available.
    """
    raw_dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not raw_dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")

    psycopg = pytest.importorskip("psycopg")

    schema = f"test_{uuid4().hex[:8]}"

    with psycopg.connect(raw_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')

    schema_sql = _load_app_schema_sql()

    with psycopg.connect(raw_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)
        conn.commit()

    yield raw_dsn, schema

    with psycopg.connect(raw_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _pg_db_fixture(pg_dsn: tuple[str, str]):  # noqa: ANN201
    """Function-scoped PostgresDatabase that truncates tables after each test."""
    from rescue_ai.infrastructure.postgres_connection import PostgresDatabase

    dsn, schema = pg_dsn
    db = PostgresDatabase(dsn=dsn, schema=schema)
    yield db
    db.truncate_all()


pg_db = pytest.fixture(name="pg_db")(_pg_db_fixture)
