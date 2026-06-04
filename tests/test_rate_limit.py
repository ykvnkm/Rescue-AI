"""Unit tests for the in-process demo rate limiter (no TestClient)."""

from __future__ import annotations

from rescue_ai.interfaces.api.rate_limit import FixedWindowRateLimiter, client_key


def test_disabled_when_limit_non_positive() -> None:
    limiter = FixedWindowRateLimiter(0)
    assert limiter.enabled is False
    # Always allowed regardless of how many calls.
    assert all(limiter.allow("ip", now=float(i)) for i in range(1000))


def test_allows_up_to_limit_then_blocks_within_window() -> None:
    limiter = FixedWindowRateLimiter(3)
    assert limiter.enabled is True
    assert [limiter.allow("ip", now=0.0) for _ in range(5)] == [
        True,
        True,
        True,
        False,
        False,
    ]


def test_window_resets_after_a_minute() -> None:
    limiter = FixedWindowRateLimiter(2)
    assert limiter.allow("ip", now=0.0) is True
    assert limiter.allow("ip", now=1.0) is True
    assert limiter.allow("ip", now=2.0) is False  # over the cap in-window
    # 60s later the window rolls over.
    assert limiter.allow("ip", now=61.0) is True


def test_keys_are_isolated_per_client() -> None:
    limiter = FixedWindowRateLimiter(1)
    assert limiter.allow("a", now=0.0) is True
    assert limiter.allow("a", now=0.0) is False
    # A different client has its own budget.
    assert limiter.allow("b", now=0.0) is True


def test_client_key_prefers_forwarded_for() -> None:
    assert client_key(forwarded_for="1.2.3.4, 10.0.0.1", peer="10.0.0.1") == "1.2.3.4"
    assert client_key(forwarded_for=None, peer="10.0.0.1") == "10.0.0.1"
    assert client_key(forwarded_for="", peer=None) == "unknown"
