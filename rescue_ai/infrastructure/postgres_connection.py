"""Postgres connection infrastructure: readiness checks and database wrapper."""

from __future__ import annotations

import importlib
import threading
import time
from functools import lru_cache
from typing import Any, Literal
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

    conninfo_to_dict = getattr(conninfo, "conninfo_to_dict", None)
    programming_error = getattr(psycopg, "ProgrammingError", None)
    if conninfo_to_dict is None or programming_error is None:
        return False

    try:
        conninfo_to_dict(
            "postgresql://user:secret@localhost:5432/rescue_ai?sslnegotiation=postgres"
        )
    except programming_error:  # pragma: no cover - depends on local libpq build
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
        # One reused connection PER THREAD. Opening a fresh connection on every
        # repository call costs a full TCP+auth round-trip — negligible against
        # a local Postgres (~1 ms) but ~30-80 ms against a remote managed DB,
        # which dominated per-frame auto-mode latency (2 connects/frame ≈ 80 ms
        # while the CPU sat idle). Reusing a per-thread connection drops repeat
        # queries to a few ms. Thread-local keeps it safe — psycopg connections
        # are not shareable across threads, and each auto-session worker / request
        # thread gets its own. See docs note on cloud auto-mode latency.
        self._tls = threading.local()

    def _acquire(self) -> Any:
        conn = getattr(self._tls, "conn", None)
        if conn is None or conn.closed:
            conn = self._psycopg.connect(
                self._dsn, connect_timeout=_CONNECT_TIMEOUT_SEC
            )
            if self._schema:
                conn.execute(f"SET search_path TO {self._schema}")
            self._tls.conn = conn
        return conn

    def _discard(self) -> None:
        conn = getattr(self._tls, "conn", None)
        self._tls.conn = None
        if conn is not None:
            try:
                conn.close()
            except (self._psycopg.Error, OSError):  # pragma: no cover - best-effort
                pass

    def connect(self) -> "_ReusedConnection":
        """Check out this thread's reused connection (opened on first use).

        Returned object is a context manager: ``with db.connect() as conn:``
        commits on success / rolls back on error like before, but keeps the
        connection OPEN for the next call instead of closing it.
        """
        return _ReusedConnection(self)

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


class _ReusedConnection:
    """Context manager that lends ``PostgresDatabase``'s per-thread connection
    and commits/rolls back on exit WITHOUT closing it (so it can be reused)."""

    def __init__(self, db: "PostgresDatabase") -> None:
        self._db = db
        self._conn: Any = None

    def __enter__(self) -> Any:
        self._conn = self._db._acquire()
        return self._conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        if self._conn is None:
            return False
        if exc_type is not None:
            # Roll back the failed txn; if even that fails the connection is
            # unusable — drop it so the next call opens a fresh one.
            try:
                self._conn.rollback()
            except (self._db._psycopg.Error, OSError):
                self._db._discard()
            return False
        try:
            self._conn.commit()
        except Exception:
            self._db._discard()
            raise
        return False
