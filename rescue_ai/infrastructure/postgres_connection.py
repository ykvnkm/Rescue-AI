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
    """Append a search_path override to a Postgres DSN."""
    parsed = urlparse(dsn)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    options = [value for key, value in query_items if key == "options"]
    merged_options = " ".join(
        option for option in [*options, f"-csearch_path={schema}"] if option
    )
    filtered_items = [(key, value) for key, value in query_items if key != "options"]
    filtered_items.append(("options", merged_options))
    return urlunparse(parsed._replace(query=urlencode(filtered_items)))


def wait_for_postgres(
    dsn: str,
    *,
    timeout_sec: float = 30.0,
    interval_sec: float = 1.0,
) -> None:
    """Poll the database until a simple SELECT succeeds."""
    psycopg = importlib.import_module("psycopg")

    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn) as conn:
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


class PostgresDatabase:
    """Thin wrapper around a psycopg DSN used by repository adapters."""

    def __init__(self, dsn: str) -> None:
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "psycopg is required for APP_REPOSITORY_BACKEND=postgres"
            ) from exc

        self._psycopg = psycopg
        self._dsn = dsn

    def connect(self) -> Any:
        """Open a new connection to the database."""
        return self._psycopg.connect(self._dsn)

    def truncate_all(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    TRUNCATE TABLE episodes, alerts, frame_events, missions CASCADE
                    """
                )
            conn.commit()
