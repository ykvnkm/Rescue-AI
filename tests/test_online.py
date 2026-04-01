"""Unit tests for online CLI entry point."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rescue_ai.config import get_settings


def setup_function() -> None:
    get_settings.cache_clear()


def teardown_function() -> None:
    get_settings.cache_clear()


def test_prepare_postgres_raises_when_no_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_DSN", raising=False)

    from rescue_ai.interfaces.cli import online as online_main

    class _Settings:
        class _Api:
            postgres_ready_timeout_sec = 30.0

        class _Database:
            dsn = ""

        api = _Api()
        database = _Database()

    def _fake_get_settings() -> _Settings:
        return _Settings()

    monkeypatch.setattr(online_main, "get_settings", _fake_get_settings)

    with pytest.raises(RuntimeError, match="DB_DSN is required"):
        online_main._prepare_postgres_backend()


def test_prepare_postgres_waits_with_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_DSN", "postgresql://user:pass@localhost:5432/db")
    from rescue_ai.interfaces.cli import online as online_main

    called: dict[str, object] = {}

    def _fake_wait(dsn: str, *, timeout_sec: float) -> None:
        called["dsn"] = dsn
        called["timeout_sec"] = timeout_sec

    monkeypatch.setattr(online_main, "wait_for_postgres", _fake_wait)
    online_main._prepare_postgres_backend()

    assert "postgresql://user:pass@localhost:5432/db" in str(called["dsn"])
    assert "_search_path=app" in str(called["dsn"])
    assert called["timeout_sec"] == get_settings().api.postgres_ready_timeout_sec


@patch("rescue_ai.interfaces.cli.online.uvicorn")
def test_main_calls_uvicorn_run(
    mock_uvicorn: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_DSN", "postgresql://user:pass@localhost:5432/db")
    from rescue_ai.interfaces.cli import online as online_main

    monkeypatch.setattr(online_main, "wait_for_postgres", lambda dsn, timeout_sec: None)
    monkeypatch.setattr(
        online_main,
        "build_api_runtime",
        lambda: (MagicMock(), MagicMock(), lambda: None),
    )
    from rescue_ai.interfaces.cli.online import main

    main()
    mock_uvicorn.run.assert_called_once()
    call_kwargs = mock_uvicorn.run.call_args
    assert "rescue_ai.interfaces.api.app:app" in call_kwargs.args
