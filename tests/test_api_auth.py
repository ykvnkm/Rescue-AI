"""Unit tests for the shared-secret bearer auth helper (no TestClient)."""

from __future__ import annotations

import pytest

from rescue_ai.interfaces.api.auth import is_authorized, is_open_path


def _authorized(path: str, header: str | None, token: str) -> bool:
    return is_authorized(path=path, authorization_header=header, expected_token=token)


def test_gate_disabled_when_no_token_configured() -> None:
    # Empty secret → offline/dev profile → everything passes through.
    assert _authorized("/missions/start", None, "") is True
    assert _authorized("/auto-sessions/start", "garbage", "") is True


def test_valid_bearer_token_allowed() -> None:
    assert _authorized("/s3-missions", "Bearer s3cret", "s3cret") is True
    # Scheme is case-insensitive.
    assert _authorized("/s3-missions", "bearer s3cret", "s3cret") is True


def test_wrong_or_missing_token_rejected() -> None:
    assert _authorized("/missions/start", "Bearer nope", "s3cret") is False
    assert _authorized("/missions/start", None, "s3cret") is False
    assert _authorized("/missions/start", "Basic s3cret", "s3cret") is False
    assert _authorized("/missions/start", "s3cret", "s3cret") is False


@pytest.mark.parametrize(
    "path",
    ["/", "/pilot", "/favicon.ico", "/health", "/metrics", "/docs", "/openapi.json"],
)
def test_open_paths_bypass_auth(path: str) -> None:
    assert is_open_path(path) is True
    # Open paths pass even with a configured token and no Authorization header.
    assert _authorized(path, None, "s3cret") is True


def test_protected_paths_are_not_open() -> None:
    assert is_open_path("/missions/start") is False
    assert is_open_path("/s3-missions") is False
