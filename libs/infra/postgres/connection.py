from __future__ import annotations

import os
import time
from collections.abc import Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse


_FATAL_SQLSTATES = {
    "28P01",  # invalid_password
    "28000",  # invalid_authorization_specification
    "3D000",  # invalid_catalog_name (database does not exist)
}


def resolve_postgres_dsn(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve a Postgres DSN from APP_POSTGRES_* environment variables."""
    values = os.environ if environ is None else environ

    raw_dsn = _clean(values.get("APP_POSTGRES_DSN"))
    if raw_dsn is not None:
        _validate_postgres_dsn(raw_dsn)
        return raw_dsn

    host = _clean(values.get("APP_POSTGRES_HOST"))
    port = _clean(values.get("APP_POSTGRES_PORT"))
    database = _clean(values.get("APP_POSTGRES_DB"))
    user = _clean(values.get("APP_POSTGRES_USER"))
    password = _clean(values.get("APP_POSTGRES_PASSWORD"))

    has_component_values = any(
        value is not None for value in (host, port, database, user, password)
    )
    if not has_component_values:
        return None

    missing = [
        name
        for name, value in (
            ("APP_POSTGRES_HOST", host),
            ("APP_POSTGRES_PORT", port),
            ("APP_POSTGRES_DB", database),
            ("APP_POSTGRES_USER", user),
            ("APP_POSTGRES_PASSWORD", password),
        )
        if value is None
    ]
    if missing:
        missing_list = ", ".join(missing)
        raise ValueError(
            "Incomplete Postgres settings. "
            "Set APP_POSTGRES_DSN or all of: "
            "APP_POSTGRES_HOST, APP_POSTGRES_PORT, APP_POSTGRES_DB, "
            "APP_POSTGRES_USER, APP_POSTGRES_PASSWORD. "
            f"Missing: {missing_list}"
        )

    return (
        "postgresql://"
        f"{quote(user or '', safe='')}:{quote(password or '', safe='')}"
        f"@{host}:{port}/{quote(database or '', safe='')}"
    )


def to_sqlalchemy_url(dsn: str) -> str:
    """Convert a psycopg-style DSN to the SQLAlchemy URL Alembic expects."""
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+psycopg://", 1)
    return dsn


def dsn_with_search_path(dsn: str, schema: str) -> str:
    """Append a search_path override to a Postgres DSN."""
    parsed = urlparse(dsn)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    options = [value for key, value in query_items if key == "options"]
    merged_options = " ".join(
        option for option in [*options, f"-csearch_path={schema}"] if option
    )
    filtered_items = [(key, value) for key, value in query_items if key != "options"]
    filtered_items.append(("options", merged_options))
    return urlunparse(parsed._replace(query=urlencode(filtered_items)))


def wait_for_postgres(
    dsn: str,
    *,
    timeout_sec: float = 30.0,
    interval_sec: float = 1.0,
) -> None:
    """Poll the database until a simple SELECT succeeds."""
    import psycopg

    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn) as conn:
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


def _validate_postgres_dsn(dsn: str) -> None:
    parsed = urlparse(dsn)

    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError(
            "APP_POSTGRES_DSN must start with postgresql:// or postgres://"
        )
    if not parsed.hostname:
        raise ValueError("APP_POSTGRES_DSN must include host")
    if not parsed.username:
        raise ValueError("APP_POSTGRES_DSN must include user")
    if parsed.password in (None, ""):
        raise ValueError("APP_POSTGRES_DSN must include non-empty password")
    if not parsed.path or parsed.path == "/":
        raise ValueError("APP_POSTGRES_DSN must include database name")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None