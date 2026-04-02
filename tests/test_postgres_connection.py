from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from rescue_ai.config import DatabaseSettings
from rescue_ai.infrastructure.postgres_connection import (
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
