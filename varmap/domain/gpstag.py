"""Extract positions from free text: VarAC <GPS:...> tags, <LOC:grid> tags and
bare grid locators, as found in VMails, broadcasts and the datastream.

VarAC display-mangles text in some tables: digit 0 -> Ø, '<' -> «,
'>' -> ».  Received broadcasts carry the guillemets too.  Every parser
here accepts both the clean and the mangled forms.

Observed <GPS:...> payload variants (all from a live database):
    33.86000,-84.30000
    38.00977 N 78.83755W
    38°31.6257 N 077°13.4588 W
    46° 32.616' N  87° 32.616' W
There is no canonical format: it is whatever the sending operator's GPS or
manual entry produced.  Be tolerant, but return None rather than guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from .grid import grid_accuracy_m, grid_to_latlon, normalise_grid

ZERO_SLASH = "Ø"
GUILLEMET_OPEN = "«"
GUILLEMET_CLOSE = "»"

_OPEN = "[<«]"
_CLOSE = "[>»]"

GPS_TAG = re.compile(_OPEN + r"\s*GPS\s*:\s*([^>»]+?)\s*" + _CLOSE, re.I)
LOC_TAG = re.compile(
    _OPEN + r"\s*LOC(?:ATOR)?\s*:\s*([A-R]{2}[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?)\s*" + _CLOSE, re.I)

# A bare 6-character grid delimited by non-alphanumerics: "(DM33vo)", "Loc :FK94lp".
# 6 characters minimum -- a bare 4-char grid is too ambiguous in free text.
BARE_GRID = re.compile(r"(?<![A-Z0-9])([A-R]{2}[0-9]{2}[A-X]{2})(?![A-Z0-9])", re.I)

DEC_PAIR = re.compile(r"^\s*([+-]?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$")
# Degrees [minutes [seconds]] + hemisphere: 38°31.6257 N / 46° 32.616' N / 38°31'22.5"N
DMS_ONE = re.compile(
    r"(\d{1,3})\s*[°d\s]\s*(\d{1,2}(?:\.\d+)?)\s*['′’]?\s*"
    r"(?:(\d{1,2}(?:\.\d+)?)\s*[\"″”]\s*)?([NSEW])", re.I)
# Hemisphere-first: N38 31.6257 W077 13.4588
DMS_HEMI_FIRST = re.compile(
    r"([NSEW])\s*(\d{1,3})\s*[°d\s]\s*(\d{1,2}(?:\.\d+)?)\s*['′]?", re.I)
# Decimal degrees + hemisphere: 38.00977 N 78.83755W
HEMI_DEC = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*([NSEW])(?![A-Z])", re.I)


def unmangle(s: Optional[str]) -> str:
    """Undo VarAC's display substitutions.  Lossy for a genuine Ø, so only
    apply to text you are about to parse for numbers/tags, never to text you show."""
    if not s:
        return ""
    return s.replace(ZERO_SLASH, "0").replace(GUILLEMET_OPEN, "<").replace(GUILLEMET_CLOSE, ">")


def _validate(lat: float, lon: float) -> Optional[Tuple[float, float]]:
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return None  # null island: GPS no-fix artefact
    return lat, lon


def _assign(vals: dict, value: float, hemi: str) -> None:
    h = hemi.upper()
    if h in ("S", "W"):
        value = -value
    vals["lat" if h in ("N", "S") else "lon"] = value


def parse_coordinate_text(payload: str) -> Optional[Tuple[float, float]]:
    """Parse the inside of a <GPS:...> tag (or any bare coordinate string)."""
    if not payload:
        return None
    p = unmangle(payload).strip()

    dms = DMS_ONE.findall(p)
    if len(dms) == 2:
        vals: dict = {}
        for deg, mins, secs, hemi in dms:
            v = int(deg) + float(mins) / 60.0 + (float(secs) / 3600.0 if secs else 0.0)
            _assign(vals, v, hemi)
        if "lat" in vals and "lon" in vals:
            return _validate(vals["lat"], vals["lon"])

    hf = DMS_HEMI_FIRST.findall(p)
    if len(hf) == 2:
        vals = {}
        for hemi, deg, mins in hf:
            _assign(vals, int(deg) + float(mins) / 60.0, hemi)
        if "lat" in vals and "lon" in vals:
            return _validate(vals["lat"], vals["lon"])

    if "°" not in p:
        hd = HEMI_DEC.findall(p)
        if len(hd) == 2:
            vals = {}
            for num, hemi in hd:
                _assign(vals, float(num), hemi)
            if "lat" in vals and "lon" in vals:
                return _validate(vals["lat"], vals["lon"])

    dp = DEC_PAIR.match(p)
    if dp:
        return _validate(float(dp.group(1)), float(dp.group(2)))
    return None


def parse_gps_tag(text: Optional[str]) -> Optional[Tuple[float, float]]:
    """Find a <GPS:...> / «GPS:...» tag in text and parse it."""
    m = GPS_TAG.search(text or "")
    if not m:
        return None
    return parse_coordinate_text(m.group(1))


@dataclass(frozen=True)
class TextPosition:
    lat: float
    lon: float
    grid: Optional[str]
    kind: str            # 'gps' | 'loc_tag' | 'bare_grid'
    accuracy_m: float
    raw: str


def parse_position_text(text: Optional[str]) -> Optional[TextPosition]:
    """Best position carried by a free-text message, or None.

    Priority: <GPS:...> (exact) > <LOC:grid> tag > bare 6-char grid.
    """
    if not text:
        return None
    m = GPS_TAG.search(text)
    if m:
        ll = parse_coordinate_text(m.group(1))
        if ll:
            return TextPosition(ll[0], ll[1], None, "gps", 10.0, m.group(0))
    m = LOC_TAG.search(text)
    if m:
        g = normalise_grid(m.group(1))
        c = grid_to_latlon(g)
        if c:
            return TextPosition(c[0], c[1], g, "loc_tag", grid_accuracy_m(g) or 3800.0, m.group(0))
    m = BARE_GRID.search(unmangle(text))
    if m:
        g = normalise_grid(m.group(1))
        c = grid_to_latlon(g)
        if c:
            return TextPosition(c[0], c[1], g, "bare_grid", 3800.0, m.group(0))
    return None


# Consent token carried in VarMap position broadcasts: 'APRS:Y' = you may relay my
# position to APRS, 'APRS:N' = you may not.  Absent = no statement (treated as no).
APRS_CONSENT = re.compile(r"(?<![A-Z0-9])APRS\s*[:=]\s*([YN])(?![A-Z0-9])", re.I)
APRS_CONSENT_YES = "APRS:Y"
APRS_CONSENT_NO = "APRS:N"


def parse_aprs_consent(text: Optional[str]) -> Optional[bool]:
    """True for APRS:Y, False for APRS:N, None when the message says nothing."""
    m = APRS_CONSENT.search(unmangle(text or ""))
    if not m:
        return None
    return m.group(1).upper() == "Y"


def format_gps_tag(lat: float, lon: float, decimals: int = 5) -> str:
    """The signed decimal form VarAC itself produces from ManualGPSData."""
    decimals = max(0, min(int(decimals), 7))
    return f"<GPS:{lat:.{decimals}f},{lon:.{decimals}f}>"
