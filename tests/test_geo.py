"""Unit tests for local ENU → geographic conversion."""

from __future__ import annotations

from rescue_ai.domain.geo import is_valid_lat, is_valid_lon, relative_to_absolute


def test_zero_offset_returns_origin() -> None:
    lat, lon = relative_to_absolute(0.0, 0.0, origin_lat=55.75, origin_lon=37.62)
    assert (round(lat, 9), round(lon, 9)) == (55.75, 37.62)


def test_north_offset_increases_latitude() -> None:
    # ~111_320 m per degree of latitude → 1000 m north ≈ +0.00898°.
    lat, lon = relative_to_absolute(0.0, 1000.0, origin_lat=0.0, origin_lon=0.0)
    assert abs(lat - 0.008983) < 1e-5
    assert abs(lon) < 1e-9


def test_east_offset_scales_with_latitude() -> None:
    # The same eastward meters shift longitude more near the poles.
    _, lon_equator = relative_to_absolute(1000.0, 0.0, origin_lat=0.0, origin_lon=0.0)
    _, lon_high = relative_to_absolute(1000.0, 0.0, origin_lat=60.0, origin_lon=0.0)
    assert lon_high > lon_equator > 0


def test_validators() -> None:
    assert is_valid_lat(55.75) and is_valid_lat(-90.0) and is_valid_lat(90.0)
    assert not is_valid_lat(90.1)
    assert is_valid_lon(37.62) and is_valid_lon(-180.0) and is_valid_lon(180.0)
    assert not is_valid_lon(181.0)
