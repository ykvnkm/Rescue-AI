"""Shared-secret bearer auth for the HTTP API (cloud profile).

Minimal gateway-style authentication: a single token from
``SecuritySettings.api_auth_token``. There are no users, sessions or JWTs —
just one secret sourced from Vault/env. When the token is empty the gate is
disabled so the offline/dev profiles keep working without a token.

A small allowlist stays reachable without the token so Kubernetes probes can
check liveness, Prometheus can scrape ``/metrics``, the docs load their schema,
and the browser can fetch the UI shell (which then attaches the token to every
subsequent API call). WebSocket routes are not handled by the HTTP middleware
and are therefore implicitly exempt — they only stream a session the caller
already authenticated to start.
"""

from __future__ import annotations

import hmac

# Reachable without a token: liveness probe, Prometheus scrape, OpenAPI/docs,
# the UI shell + favicon. Everything else requires the bearer token when one is
# configured.
_OPEN_PATHS = frozenset(
    {
        "/",
        "/pilot",
        "/favicon.ico",
        "/health",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/openapi.yaml",
    }
)


def is_open_path(path: str) -> bool:
    """Return True if *path* is reachable without authentication."""
    return path in _OPEN_PATHS


def is_authorized(
    *,
    path: str,
    authorization_header: str | None,
    expected_token: str,
) -> bool:
    """Decide whether a request may proceed.

    * No configured token → the gate is disabled, always allowed.
    * Open path → allowed.
    * Otherwise require ``Authorization: Bearer <token>`` matching the secret
      (compared in constant time to avoid leaking it via timing).
    """
    if not expected_token:
        return True
    if is_open_path(path):
        return True
    token = _extract_bearer(authorization_header)
    if token is None:
        return False
    return hmac.compare_digest(token, expected_token)


def _extract_bearer(header: str | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


__all__ = ["is_authorized", "is_open_path"]
