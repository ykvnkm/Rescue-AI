from __future__ import annotations

import pytest

from libs.infra.postgres.connection import resolve_postgres_dsn


def test_resolve_postgres_dsn_returns_none_when_postgres_not_configured() -> None:
    assert resolve_postgres_dsn({}) is None


def test_resolve_postgres_dsn_requires_all_component_values() -> None:
    with pytest.raises(ValueError, match="APP_POSTGRES_PORT"):
        resolve_postgres_dsn(
            {
                "APP_POSTGRES_HOST": "localhost",
                "APP_POSTGRES_DB": "rescue_ai",
                "APP_POSTGRES_USER": "rescue_ai",
                "APP_POSTGRES_PASSWORD": "secret",
            }
        )


def test_resolve_postgres_dsn_rejects_blank_password() -> None:
    with pytest.raises(ValueError, match="APP_POSTGRES_PASSWORD"):
        resolve_postgres_dsn(
            {
                "APP_POSTGRES_HOST": "localhost",
                "APP_POSTGRES_PORT": "5432",
                "APP_POSTGRES_DB": "rescue_ai",
                "APP_POSTGRES_USER": "rescue_ai",
                "APP_POSTGRES_PASSWORD": "   ",
            }
        )


def test_resolve_postgres_dsn_rejects_dsn_without_password() -> None:
    with pytest.raises(ValueError, match="non-empty password"):
        resolve_postgres_dsn(
            {
                "APP_POSTGRES_DSN": "postgresql://rescue_ai@localhost:5432/rescue_ai",
            }
        )


def test_resolve_postgres_dsn_returns_valid_component_based_dsn() -> None:
    dsn = resolve_postgres_dsn(
        {
            "APP_POSTGRES_HOST": "localhost",
            "APP_POSTGRES_PORT": "5432",
            "APP_POSTGRES_DB": "rescue_ai",
            "APP_POSTGRES_USER": "rescue_ai",
            "APP_POSTGRES_PASSWORD": "secret",
        }
    )
    assert dsn == "postgresql://rescue_ai:secret@localhost:5432/rescue_ai"
