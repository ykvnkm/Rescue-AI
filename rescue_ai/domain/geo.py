"""Local ENU ↔ geographic conversion for reconstructed trajectories.

The trajectory reconstruction yields displacement in **meters relative to the
mission origin** (the first calibrated frame). When the operator provides an
absolute start point (latitude/longitude), those relative offsets can be
projected back onto geographic coordinates with an equirectangular
small-area approximation — accurate to well under a metre over the few
hundred metres a search flight covers, and free of any map/projection deps.

Assumption: the local frame is East-North-Up, i.e. ``x`` points East and
``y`` points North (meters). That matches how the navigation engine reports
horizontal displacement; altitude ``z`` is left as-is (meters).
"""

from __future__ import annotations

import math

_EARTH_RADIUS_M = 6_378_137.0


def relative_to_absolute(
    x_east_m: float,
    y_north_m: float,
    *,
    origin_lat: float,
    origin_lon: float,
) -> tuple[float, float]:
    """Project a local ENU offset (meters) onto ``(lat, lon)`` degrees."""
    d_lat = math.degrees(y_north_m / _EARTH_RADIUS_M)
    cos_lat = math.cos(math.radians(origin_lat))
    # Guard the poles where cos(lat) → 0; search flights never run there, but
    # keep the math finite rather than dividing by zero.
    if abs(cos_lat) < 1e-12:
        d_lon = 0.0
    else:
        d_lon = math.degrees(x_east_m / (_EARTH_RADIUS_M * cos_lat))
    return origin_lat + d_lat, origin_lon + d_lon


def is_valid_lat(value: float) -> bool:
    """True if *value* is a valid latitude in degrees."""
    return -90.0 <= value <= 90.0


def is_valid_lon(value: float) -> bool:
    """True if *value* is a valid longitude in degrees."""
    return -180.0 <= value <= 180.0


__all__ = ["relative_to_absolute", "is_valid_lat", "is_valid_lon"]
