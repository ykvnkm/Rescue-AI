"""In-process per-client rate limiter for the open public demo.

A fixed-window counter keyed by client IP. The public demo runs open (no
bearer token), so this caps abuse — request floods, rapid session cycling —
without identifying users. Disabled when the configured limit is ``<= 0``;
controlled deployments rely on the bearer gate / ingress instead.

Single-replica friendly: state lives in process memory, which is the right
scope for a one-pod demo. For a multi-replica deployment move the limit to the
ingress (e.g. nginx ``limit_req``) or a shared store — documented in the
runbook rather than baked in here.
"""

from __future__ import annotations

import threading

_WINDOW_SEC = 60.0


class FixedWindowRateLimiter:
    """Allow up to ``limit_per_min`` requests per key per rolling minute."""

    def __init__(self, limit_per_min: int) -> None:
        self._limit = int(limit_per_min)
        self._lock = threading.Lock()
        # key -> (window_start_monotonic, count_in_window)
        self._counters: dict[str, tuple[float, int]] = {}

    @property
    def enabled(self) -> bool:
        """True when a positive limit is configured."""
        return self._limit > 0

    def allow(self, key: str, now: float) -> bool:
        """Record a request for *key* at time *now* and return whether to allow.

        *now* is a monotonic timestamp (seconds). Each key gets a fresh window
        once ``_WINDOW_SEC`` has elapsed since its window opened.
        """
        if self._limit <= 0:
            return True
        with self._lock:
            window_start, count = self._counters.get(key, (now, 0))
            if now - window_start >= _WINDOW_SEC:
                window_start, count = now, 0
            count += 1
            self._counters[key] = (window_start, count)
            return count <= self._limit


def client_key(*, forwarded_for: str | None, peer: str | None) -> str:
    """Resolve the rate-limit key, honouring ``X-Forwarded-For`` behind a proxy.

    Behind an ingress, the socket peer is the proxy, so the real client lives
    in the first ``X-Forwarded-For`` hop. Falls back to the socket peer.
    """
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return peer or "unknown"


__all__ = ["FixedWindowRateLimiter", "client_key"]
