"""Postgres connection infrastructure: readiness checks and database wrapper."""

from __future__ import annotations

import importlib
import time
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

_FATAL_SQLSTATES = {
    "28P01",  # invalid_password
    "28000",  # invalid_authorization_specification
    "3D000",  # invalid_catalog_name (database does not exist)
}
_CONNECT_TIMEOUT_SEC = 10


@lru_cache(maxsize=1)
def _supports_sslnegotiation() -> bool:
    """Return whether current psycopg/libpq accepts sslnegotiation in DSN."""
    try:
        psycopg = importlib.import_module("psycopg")
        conninfo = importlib.import_module("psycopg.conninfo")
    except ImportError:  # pragma: no cover - optional dependency
        return False

    try:
        conninfo.conninfo_to_dict(
            "postgresql://user:secret@localhost:5432/rescue_ai?sslnegotiation=postgres"
        )
    except psycopg.ProgrammingError:  # pragma: no cover - depends on local libpq build
        return False

    return True


def _ensure_compat_dsn(dsn: str) -> str:
    """Ensure DSN uses legacy SSL negotiation for Supabase pooler compat.

    psycopg 3.2+ defaults to ``sslnegotiation=direct`` which causes
    silent hangs with Supabase Supavisor (both transaction and session
    pooler modes).  This helper injects ``sslnegotiation=postgres`` when
    the parameter is not already present and the local psycopg/libpq build
    supports that option.
    """
    parsed = urlparse(dsn)
    params = parse_qs(parsed.query, keep_blank_values=True)

    updated = False
    if "sslnegotiation" not in params and _supports_sslnegotiation():
        params["sslnegotiation"] = ["postgres"]
        updated = True

    if "connect_timeout" not in params:
        params["connect_timeout"] = [str(_CONNECT_TIMEOUT_SEC)]
        updated = True

    if not updated:
        return dsn

    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def wait_for_postgres(
    dsn: str,
    *,
    timeout_sec: float = 30.0,
    interval_sec: float = 1.0,
) -> None:
    """Poll the database until a simple SELECT succeeds."""
    psycopg = importlib.import_module("psycopg")
    safe_dsn = _ensure_compat_dsn(dsn)

    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with psycopg.connect(
                safe_dsn, connect_timeout=_CONNECT_TIMEOUT_SEC
            ) as conn:
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

    def __init__(self, dsn: str, *, schema: str | None = None) -> None:
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for Postgres repositories") from exc

        self._psycopg = psycopg
        self._dsn = _ensure_compat_dsn(dsn)
        self._schema = schema

    def connect(self) -> Any:
        """Open a new connection and apply search_path if configured."""
        conn = self._psycopg.connect(self._dsn, connect_timeout=_CONNECT_TIMEOUT_SEC)
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
