"""Tests for PostgreSQL connection and DSN handling."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from rescue_ai.config import DatabaseSettings
from rescue_ai.infrastructure.postgres_connection import (
    PostgresDatabase,
    _ensure_compat_dsn,
    _supports_sslnegotiation,
)


def test_dsn_defaults_to_empty() -> None:
    settings = DatabaseSettings(DB_DSN="")
    assert settings.dsn == ""


def test_dsn_reads_value() -> None:
    settings = DatabaseSettings(
        DB_DSN="postgresql://user:secret@localhost:5432/rescue_ai",
    )
    assert settings.dsn == "postgresql://user:secret@localhost:5432/rescue_ai"


def test_ensure_compat_dsn_adds_supavisor_params() -> None:
    dsn = "postgresql://user:secret@localhost:5432/rescue_ai"
    result = _ensure_compat_dsn(dsn)
    query = parse_qs(urlparse(result).query, keep_blank_values=True)

    if _supports_sslnegotiation():
        assert query["sslnegotiation"] == ["postgres"]
    else:
        assert "sslnegotiation" not in query
    assert query["connect_timeout"] == ["10"]


def test_ensure_compat_dsn_preserves_existing_params() -> None:
    dsn = (
        "postgresql://user:secret@localhost:5432/rescue_ai"
        "?sslnegotiation=postgres&connect_timeout=3"
    )
    result = _ensure_compat_dsn(dsn)
    query = parse_qs(urlparse(result).query, keep_blank_values=True)

    assert query["sslnegotiation"] == ["postgres"]
    assert query["connect_timeout"] == ["3"]


class _FakeConn:
    """Minimal stand-in for a psycopg connection used to test reuse."""

    def __init__(self) -> None:
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.executed: list[str] = []
        self.rollback_error: Exception | None = None

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self) -> None:
        self.closes += 1
        self.closed = True


class _FakePsycopg:
    """Stub psycopg module: hands out fresh ``_FakeConn`` per connect()."""

    class Error(Exception):
        """Stand-in for ``psycopg.Error`` (base of all psycopg errors)."""

    def __init__(self) -> None:
        self.opened: list[_FakeConn] = []

    def connect(self, *args: object, **kwargs: object) -> _FakeConn:
        del args, kwargs  # signature mirrors psycopg.connect; values unused
        conn = _FakeConn()
        self.opened.append(conn)
        return conn


def _db_with_fake(
    monkeypatch: pytest.MonkeyPatch, schema: str | None = None
) -> tuple[PostgresDatabase, _FakePsycopg]:
    db = PostgresDatabase("postgresql://u:p@localhost:5432/db", schema=schema)
    fake = _FakePsycopg()
    monkeypatch.setattr(db, "_psycopg", fake)
    return db, fake


def test_connection_is_reused_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    db, fake = _db_with_fake(monkeypatch)
    with db.connect() as conn1:
        assert isinstance(conn1, _FakeConn)
    with db.connect() as conn2:
        pass
    assert conn1 is conn2  # same connection reused, not reopened
    assert len(fake.opened) == 1
    assert conn1.commits == 2  # committed on each successful exit
    assert conn1.closes == 0  # kept open for reuse


def test_schema_search_path_applied_once(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = _db_with_fake(monkeypatch, schema="app")
    with db.connect() as conn1:
        pass
    with db.connect() as conn2:
        pass
    assert conn1 is conn2
    assert conn1.executed == ["SET search_path TO app"]  # only on first open


def test_error_rolls_back_and_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = _db_with_fake(monkeypatch)
    with pytest.raises(ValueError):
        with db.connect() as conn:
            raise ValueError("boom")
    assert conn.rollbacks == 1
    assert conn.commits == 0
    with db.connect() as conn2:  # rollback ok → connection kept and reused
        pass
    assert conn2 is conn


def test_closed_connection_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    db, fake = _db_with_fake(monkeypatch)
    with db.connect() as conn1:
        pass
    conn1.closed = True  # simulate a dropped uplink
    with db.connect() as conn2:
        pass
    assert conn2 is not conn1
    assert len(fake.opened) == 2


def test_failed_rollback_discards_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, fake = _db_with_fake(monkeypatch)
    with db.connect() as conn1:
        pass
    conn1.rollback_error = _FakePsycopg.Error("rollback failed")
    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            assert conn is conn1
            raise RuntimeError("boom")
    assert conn1.closes == 1  # discarded after the rollback itself failed
    with db.connect() as conn2:  # next call opens a fresh connection
        pass
    assert conn2 is not conn1
    assert len(fake.opened) == 2
