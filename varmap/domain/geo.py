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


# Amateur bands by frequency (Hz), generous edges so any VarAC frequency maps.
_BANDS = [
    (1_800_000, 2_000_000, "160m"), (3_500_000, 4_000_000, "80m"), (5_250_000, 5_450_000, "60m"),
    (7_000_000, 7_300_000, "40m"), (10_100_000, 10_150_000, "30m"), (14_000_000, 14_350_000, "20m"),
    (18_068_000, 18_168_000, "17m"), (21_000_000, 21_450_000, "15m"), (24_890_000, 24_990_000, "12m"),
    (28_000_000, 29_700_000, "10m"), (50_000_000, 54_000_000, "6m"), (144_000_000, 148_000_000, "2m"),
    (222_000_000, 225_000_000, "1.25m"), (420_000_000, 450_000_000, "70cm"),
]


def freq_to_band(hz: Optional[int]) -> Optional[str]:
    """7090250 -> '40m'.  None when unknown."""
    if not hz:
        return None
    for lo, hi, name in _BANDS:
        if lo <= hz <= hi:
            return name
    return None


def normalise_band(b: str) -> str:
    """'40M', ' 40 m' -> '40m'."""
    return (b or "").strip().lower().replace(" ", "")


def implied_speed_kmh(lat1, lon1, t1, lat2, lon2, t2) -> Optional[float]:
    """Speed implied by two timed positions; None if the interval is degenerate."""
    dt = (t2 - t1).total_seconds()
    if dt <= 0:
        return None
    return haversine_km(lat1, lon1, lat2, lon2) / (dt / 3600.0)
