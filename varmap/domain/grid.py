"""Maidenhead grid locator codec.

VarAC advanced beacons carry a 6-character grid (e.g. EM73UU), optionally
suffixed with " ⌛" (hourglass) meaning the operator is away.  A 6-char
cell is 2.5' of latitude by 5' of longitude: about 4.6 km x 7.7 km at 34N.
Positions derived from a grid are cell CENTROIDS and must never be presented
as precise.
"""
from __future__ import annotations

import math
import re
from typing import Optional, Tuple

AWAY_MARK = "⌛"  # hourglass appended by VarAC when the operator is "away"

# 4, 6 or 8 character locators.  Fields A-R, squares 0-9, subsquares A-X,
# extended squares 0-9.
GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?$")

# Cell sizes at each precision, in degrees.
_LAT_CELL = {4: 1.0, 6: 1.0 / 24.0, 8: 1.0 / 240.0}
_LON_CELL = {4: 2.0, 6: 2.0 / 24.0, 8: 2.0 / 240.0}

# Conservative worst-case error (m) of using the centroid, at mid latitudes.
GRID_ACCURACY_M = {4: 90_000.0, 6: 3_800.0, 8: 400.0}


def split_locator(raw: Optional[str]) -> Tuple[str, bool]:
    """'EM73UU ⌛' -> ('EM73UU', True).  Strips the away mark and whitespace."""
    if not raw:
        return "", False
    s = str(raw).strip()
    away = AWAY_MARK in s
    grid = s.replace(AWAY_MARK, "").strip().upper()
    return grid, away


def normalise_grid(grid: Optional[str]) -> Optional[str]:
    """Return the canonical upper-case grid, or None if it is not a valid locator."""
    if not grid:
        return None
    g = str(grid).strip().upper()
    return g if GRID_RE.match(g) else None


def is_valid_grid(grid: Optional[str]) -> bool:
    return normalise_grid(grid) is not None


def grid_to_latlon(grid: Optional[str]) -> Optional[Tuple[float, float]]:
    """Grid -> (lat, lon) of the cell CENTRE.  None if invalid."""
    g = normalise_grid(grid)
    if not g:
        return None
    n = len(g)
    lon = (ord(g[0]) - 65) * 20.0 - 180.0
    lat = (ord(g[1]) - 65) * 10.0 - 90.0
    if n >= 4:
        lon += (ord(g[2]) - 48) * 2.0
        lat += (ord(g[3]) - 48) * 1.0
    if n >= 6:
        lon += (ord(g[4]) - 65) * (2.0 / 24.0)
        lat += (ord(g[5]) - 65) * (1.0 / 24.0)
    if n >= 8:
        lon += (ord(g[6]) - 48) * (2.0 / 240.0)
        lat += (ord(g[7]) - 48) * (1.0 / 240.0)
    lon += _LON_CELL[n] / 2.0
    lat += _LAT_CELL[n] / 2.0
    return lat, lon


def grid_bounds(grid: Optional[str]) -> Optional[Tuple[float, float, float, float]]:
    """Grid -> (south, west, north, east) of the cell, for drawing the rectangle."""
    g = normalise_grid(grid)
    c = grid_to_latlon(g)
    if not c:
        return None
    lat, lon = c
    hl, hn = _LAT_CELL[len(g)] / 2.0, _LON_CELL[len(g)] / 2.0
    return (lat - hl, lon - hn, lat + hl, lon + hn)


def grid_accuracy_m(grid: Optional[str]) -> Optional[float]:
    g = normalise_grid(grid)
    return GRID_ACCURACY_M.get(len(g)) if g else None


def latlon_to_grid(lat: float, lon: float, precision: int = 6) -> Optional[str]:
    """(lat, lon) -> grid at 4, 6 or 8 characters.  None if out of range."""
    if precision not in (4, 6, 8):
        raise ValueError("precision must be 4, 6 or 8")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    # Nudge the poles / antimeridian into the last cell rather than overflowing.
    la = min(lat + 90.0, 179.999999)
    lo = min(lon + 180.0, 359.999999)
    out = []
    fl, fo = int(lo // 20), int(la // 10)
    out.append(chr(65 + fl))
    out.append(chr(65 + fo))
    lo -= fl * 20
    la -= fo * 10
    if precision >= 4:
        sl, so = int(lo // 2), int(la // 1)
        out.append(str(sl))
        out.append(str(so))
        lo -= sl * 2
        la -= so * 1
    if precision >= 6:
        ul, uo = int(lo / (2.0 / 24.0)), int(la / (1.0 / 24.0))
        ul, uo = min(ul, 23), min(uo, 23)
        out.append(chr(65 + ul))
        out.append(chr(65 + uo))
        lo -= ul * (2.0 / 24.0)
        la -= uo * (1.0 / 24.0)
    if precision >= 8:
        el, eo = int(lo / (2.0 / 240.0)), int(la / (1.0 / 240.0))
        out.append(str(min(el, 9)))
        out.append(str(min(eo, 9)))
    return "".join(out)


def distance_inside_cell_m(lat: float, lon: float, grid: str) -> Optional[float]:
    """Distance (m) from the point to the NEAREST EDGE of the grid cell.

    Used for boundary hysteresis: a fix that is only 50 m inside a new cell is
    not yet believed to have crossed.  Negative if the point is outside.
    """
    b = grid_bounds(grid)
    if not b:
        return None
    s, w, n, e = b
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat))
    return min((lat - s) * m_per_deg_lat, (n - lat) * m_per_deg_lat,
               (lon - w) * m_per_deg_lon, (e - lon) * m_per_deg_lon)
