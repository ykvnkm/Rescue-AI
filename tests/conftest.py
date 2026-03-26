"""Pytest configuration: Postgres fixtures and default env setup."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    """Session-scoped Postgres DSN with an isolated test schema.

    Reads ``TEST_POSTGRES_DSN`` from environment.  If absent the fixture
    calls ``pytest.skip`` so integration tests are silently skipped when
    no database is available.
    """
    raw_dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not raw_dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")

    psycopg = pytest.importorskip("psycopg")

    from rescue_ai.infrastructure.postgres_connection import (
        dsn_with_search_path,
        ensure_schema,
    )

    schema = f"test_{uuid4().hex[:8]}"

    with psycopg.connect(raw_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')

    schema_dsn = dsn_with_search_path(raw_dsn, schema)
    ensure_schema(schema_dsn)

    yield schema_dsn

    with psycopg.connect(raw_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture()
def pg_db(pg_dsn: str):  # noqa: ANN201
    """Function-scoped PostgresDatabase that truncates tables after each test."""
    from rescue_ai.infrastructure.postgres_repositories import PostgresDatabase

    db = PostgresDatabase(dsn=pg_dsn)
    yield db
    db.truncate_all()
