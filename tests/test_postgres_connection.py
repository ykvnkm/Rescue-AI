from __future__ import annotations

import pytest

from rescue_ai.infrastructure.postgres_connection import resolve_postgres_dsn


def test_resolve_postgres_dsn_returns_none_when_not_configured() -> None:
    assert resolve_postgres_dsn({}) is None


def test_resolve_postgres_dsn_reads_db_dsn() -> None:
    dsn = resolve_postgres_dsn(
        {"DB_DSN": "postgresql://user:secret@localhost:5432/rescue_ai"}
    )
    assert dsn == "postgresql://user:secret@localhost:5432/rescue_ai"


def test_resolve_postgres_dsn_rejects_missing_password() -> None:
    with pytest.raises(ValueError, match="non-empty password"):
        resolve_postgres_dsn({"DB_DSN": "postgresql://user@localhost:5432/db"})
