"""Postgres connection infrastructure: readiness checks and database wrapper."""

from __future__ import annotations

import importlib
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_FATAL_SQLSTATES = {
    "28P01",  # invalid_password
    "28000",  # invalid_authorization_specification
    "3D000",  # invalid_catalog_name (database does not exist)
}


def dsn_with_search_path(dsn: str, schema: str) -> str:
    """Return a DSN tagged with a schema for use by PostgresDatabase.

    Does NOT embed ``options=-csearch_path=...`` into the DSN because
    PgBouncer in transaction mode (Supabase pooler) silently hangs on
    the ``options`` startup parameter.  Instead, the schema is stored as
    a query-string hint ``_search_path=<schema>`` and applied via
    ``SET search_path`` after each connection is opened — see
    :pymethod:`PostgresDatabase.connect`.
    """
    parsed = urlparse(dsn)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in query_items if k != "_search_path"]
    filtered.append(("_search_path", schema))
    return urlunparse(parsed._replace(query=urlencode(filtered)))


def wait_for_postgres(
    dsn: str,
    *,
    timeout_sec: float = 30.0,
    interval_sec: float = 1.0,
) -> None:
    """Poll the database until a simple SELECT succeeds."""
    psycopg = importlib.import_module("psycopg")
    clean_dsn, _ = _strip_search_path(dsn)

    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with psycopg.connect(clean_dsn) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
            return
        except psycopg.Error as error:
            sqlstate = getattr(error, "sqlstate", None)
            if sqlstate in _FATAL_SQLSTATES:
                raise RuntimeError(
                    "Postgres bootstrap failed due to invalid credentials "
                    f"or database settings: {type(error).__name__}: {error}"
                ) from error

            last_error = error
            time.sleep(interval_sec)

    if last_error is None:
        raise TimeoutError("Timed out waiting for PostgreSQL")

    raise TimeoutError(
        f"Timed out waiting for PostgreSQL: {type(last_error).__name__}: {last_error}"
    ) from last_error


def _strip_search_path(dsn: str) -> tuple[str, str | None]:
    """Remove ``_search_path`` hint from DSN, return (clean_dsn, schema)."""
    parsed = urlparse(dsn)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    schema: str | None = None
    clean_items: list[tuple[str, str]] = []
    for key, value in query_items:
        if key == "_search_path":
            schema = value
        else:
            clean_items.append((key, value))
    clean_dsn = urlunparse(parsed._replace(query=urlencode(clean_items)))
    return clean_dsn, schema


class PostgresDatabase:
    """Thin wrapper around a psycopg DSN used by repository adapters."""

    def __init__(self, dsn: str) -> None:
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for Postgres repositories") from exc

        clean_dsn, schema = _strip_search_path(dsn)
        self._psycopg = psycopg
        self._dsn = clean_dsn
        self._schema = schema

    def connect(self) -> Any:
        """Open a new connection and apply search_path if configured."""
        conn = self._psycopg.connect(self._dsn)
        if self._schema:
            conn.execute(f"SET search_path TO {self._schema}")
        return conn

    def truncate_all(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    TRUNCATE TABLE
                        episodes, alerts, frame_events, missions
                    CASCADE
                    """
                )
            conn.commit()
