"""Postgres DSN resolution, readiness checks, and schema bootstrap."""

from __future__ import annotations

import importlib
import os
import time
from collections.abc import Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

_FATAL_SQLSTATES = {
    "28P01",  # invalid_password
    "28000",  # invalid_authorization_specification
    "3D000",  # invalid_catalog_name (database does not exist)
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS missions (
    mission_id  TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    total_frames INTEGER NOT NULL,
    fps         DOUBLE PRECISION NOT NULL,
    completed_frame_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_missions_status ON missions (status);

CREATE TABLE IF NOT EXISTS frame_events (
    mission_id       TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
    frame_id         INTEGER NOT NULL,
    ts_sec           DOUBLE PRECISION NOT NULL,
    image_uri        TEXT NOT NULL,
    gt_person_present BOOLEAN NOT NULL,
    gt_episode_id    TEXT,
    PRIMARY KEY (mission_id, frame_id)
);
CREATE INDEX IF NOT EXISTS ix_frame_events_mission_ts
    ON frame_events (mission_id, ts_sec);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id           TEXT PRIMARY KEY,
    mission_id         TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
    frame_id           INTEGER NOT NULL,
    ts_sec             DOUBLE PRECISION NOT NULL,
    image_uri          TEXT NOT NULL,
    people_detected    INTEGER NOT NULL,
    primary_bbox       JSONB NOT NULL,
    primary_score      DOUBLE PRECISION NOT NULL,
    primary_label      TEXT NOT NULL,
    primary_model_name TEXT NOT NULL,
    primary_explanation TEXT,
    detections         JSONB NOT NULL,
    status             TEXT NOT NULL,
    reviewed_by        TEXT,
    reviewed_at_sec    DOUBLE PRECISION,
    decision_reason    TEXT,
    FOREIGN KEY (mission_id, frame_id)
        REFERENCES frame_events (mission_id, frame_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_alerts_mission_ts
    ON alerts (mission_id, ts_sec);
CREATE INDEX IF NOT EXISTS ix_alerts_mission_status
    ON alerts (mission_id, status);

CREATE TABLE IF NOT EXISTS episodes (
    mission_id     TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
    episode_index  INTEGER NOT NULL,
    start_sec      DOUBLE PRECISION NOT NULL,
    end_sec        DOUBLE PRECISION NOT NULL,
    found_by_alert BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (mission_id, episode_index)
);
CREATE INDEX IF NOT EXISTS ix_episodes_mission_found
    ON episodes (mission_id, found_by_alert);
"""


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
    psycopg = importlib.import_module("psycopg")

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


def ensure_schema(dsn: str) -> None:
    """Create all tables and indexes if they do not already exist."""
    psycopg = importlib.import_module("psycopg")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cursor:
            cursor.execute(_SCHEMA_SQL)
        conn.commit()


def _validate_postgres_dsn(dsn: str) -> None:
    """Raise ValueError if the DSN is missing required components."""
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
    """Strip whitespace, returning None for blank values."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
