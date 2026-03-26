"""Unit tests for online CLI entry point."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rescue_ai.config import get_settings


def setup_function() -> None:
    get_settings.cache_clear()


def teardown_function() -> None:
    get_settings.cache_clear()


def test_prepare_postgres_skips_when_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_REPOSITORY_BACKEND", "memory")
    from rescue_ai.interfaces.cli.online import _prepare_postgres_backend

    _prepare_postgres_backend()  # should not raise


def test_prepare_postgres_raises_when_no_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_REPOSITORY_BACKEND", "postgres")
    monkeypatch.delenv("DB_DSN", raising=False)

    from rescue_ai.interfaces.cli.online import _prepare_postgres_backend

    with pytest.raises(RuntimeError, match="Postgres backend requires"):
        _prepare_postgres_backend()


@patch("rescue_ai.interfaces.cli.online.uvicorn")
def test_main_calls_uvicorn_run(
    mock_uvicorn: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_REPOSITORY_BACKEND", "memory")
    from rescue_ai.interfaces.cli.online import main

    main()
    mock_uvicorn.run.assert_called_once()
    call_kwargs = mock_uvicorn.run.call_args
    assert "rescue_ai.interfaces.api.app:app" in call_kwargs.args
