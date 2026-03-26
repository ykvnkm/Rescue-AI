from __future__ import annotations

from rescue_ai.config import DatabaseSettings


def test_dsn_defaults_to_empty() -> None:
    settings = DatabaseSettings(DB_DSN="", BATCH_POSTGRES_DSN="")
    assert settings.dsn == ""


def test_dsn_reads_value() -> None:
    settings = DatabaseSettings(
        DB_DSN="postgresql://user:secret@localhost:5432/rescue_ai",
        BATCH_POSTGRES_DSN="",
    )
    assert settings.dsn == "postgresql://user:secret@localhost:5432/rescue_ai"
