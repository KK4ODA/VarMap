"""Great-circle maths.  Haversine is accurate to ~0.5%, three orders of
magnitude better than a grid-derived position, so no geodesic library."""
from __future__ import annotations

import math
from typing import Optional

EARTH_RADIUS_KM = 6371.0088
KM_PER_MI = 1.609344


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, 0..360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two headings, 0..180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def km_to_mi(km: float) -> float:
    return km / KM_PER_MI


def implied_speed_kmh(lat1, lon1, t1, lat2, lon2, t2) -> Optional[float]:
    """Speed implied by two timed positions; None if the interval is degenerate."""
    dt = (t2 - t1).total_seconds()
    if dt <= 0:
        return None
    return haversine_km(lat1, lon1, lat2, lon2) / (dt / 3600.0)
