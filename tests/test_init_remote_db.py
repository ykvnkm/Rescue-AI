"""Tests for init_remote_db CLI."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest


def test_init_remote_db_requires_dsn(monkeypatch) -> None:
    monkeypatch.delenv("DB_DSN", raising=False)
    from rescue_ai.interfaces.cli import init_remote_db

    class _Settings:
        class _Database:
            dsn = ""

        database = _Database()

    def _fake_get_settings() -> _Settings:
        return _Settings()

    monkeypatch.setattr(init_remote_db, "get_settings", _fake_get_settings)

    with pytest.raises(RuntimeError, match="DB_DSN is required"):
        init_remote_db.main()


def test_init_remote_db_executes_sql(monkeypatch, tmp_path: Path) -> None:
    from rescue_ai.interfaces.cli import init_remote_db

    sql_path = tmp_path / "schema.sql"
    sql_path.write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(init_remote_db, "_SQL_FILE", sql_path)

    class _Cursor:
        def __init__(self, state: dict[str, str | bool | list[str]]) -> None:
            self._state = state
            calls = state.setdefault("sql_calls", [])
            self._calls = cast(list[str], calls)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def execute(self, sql: str) -> None:
            self._calls.append(sql)

    class _Connection:
        def __init__(self, state: dict[str, str | bool | list[str]]) -> None:
            self._state = state

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def cursor(self):
            return _Cursor(self._state)

        def commit(self) -> None:
            self._state["committed"] = True

    state: dict[str, str | bool | list[str]] = {}

    class _PsycopgModule:
        @staticmethod
        def connect(dsn: str):
            state["dsn"] = dsn
            return _Connection(state)

    class _Settings:
        class _Database:
            dsn = "postgresql://u:p@h:5432/db"

        database = _Database()

    def _fake_get_settings() -> _Settings:
        return _Settings()

    def _fake_import_module(name: str):
        if name == "psycopg":
            return _PsycopgModule
        return None

    monkeypatch.setattr(init_remote_db, "get_settings", _fake_get_settings)
    monkeypatch.setattr(
        init_remote_db.importlib,
        "import_module",
        _fake_import_module,
    )

    init_remote_db.main()

    assert "postgresql://u:p@h:5432/db" in str(state["dsn"])
    assert "search_path%3Dapp" in str(state["dsn"])
    sql_calls = cast(list[str], state["sql_calls"])
    assert sql_calls[0] == "CREATE SCHEMA IF NOT EXISTS app"
    assert sql_calls[1] == "SELECT 1;"
    assert state["committed"] is True
