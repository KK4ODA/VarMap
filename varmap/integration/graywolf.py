"""Graywolf (https://github.com/chrissnell/graywolf) integration: an APRS
station (modem, digipeater, iGate) with a REST API on http://127.0.0.1:8080.

Verified against its OpenAPI spec (2026-09):
* Base path /api; cookie session from POST /api/auth/login {username, password}.
* GET /api/stations?bbox=sw_lat,sw_lon,ne_lat,ne_lon&timerange=s&since=RFC3339
  (delta) with ETag / If-None-Match; each StationDTO carries callsign, symbol,
  comment, path, last_heard, is_object and a `positions` history (newest first).
* GET /api/position: Graywolf's own fix (GPS or fixed), speed in knots, heading.
* GET /api/version, /api/health.

Receive-only in VarMap stages 1-2.  Nothing here transmits.
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..domain.callsign import normalise_callsign
from ..domain.grid import latlon_to_grid
from ..domain.timeparse import now_utc, parse_varac_time
from .contracts import Observation, OwnFix

log = logging.getLogger("varmap.graywolf")

WORLD_BBOX = "-90,-180,90,180"
KNOTS_TO_KMH = 1.852


def parse_rfc3339(s: Optional[str]) -> Optional[datetime]:
    """RFC3339 with any fraction length and Z or numeric offset -> aware UTC."""
    if not s:
        return None
    t = parse_varac_time(s)
    if t is not None:
        return t
    try:
        x = str(s).strip()
        m = re.match(r"^(.*?)(\.\d+)?(Z|[+-]\d{2}:\d{2})$", x)
        if m and m.group(2) and len(m.group(2)) > 7:
            x = m.group(1) + m.group(2)[:7] + m.group(3)
        d = datetime.fromisoformat(x.replace("Z", "+00:00"))
        return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class GraywolfError(Exception):
    pass


class GraywolfClient:
    """Small cookie-session REST client.  Thread-safe for the simple use here."""

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 10.0) -> None:
        self.base = (base_url or "http://127.0.0.1:8080").rstrip("/")
        self.username, self.password, self.timeout = username or "", password or "", timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._lock = threading.Lock()
        self.logged_in = False
        self.last_error: Optional[str] = None
        self.version: Optional[str] = None

    # -- transport ---------------------------------------------------------
    def _raw(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Any = None,
             headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any, Dict[str, str]]:
        url = f"{self.base}/api{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                payload = json.loads(raw.decode("utf-8")) if raw and "json" in hdrs.get("content-type", "") else (raw.decode("utf-8", "replace") if raw else None)
                return resp.status, payload, hdrs
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                payload = raw.decode("utf-8", "replace") if raw else None
            return e.code, payload, {k.lower(): v for k, v in e.headers.items()}

    def login(self) -> bool:
        with self._lock:
            status, payload, _ = self._raw("POST", "/auth/login", body={"username": self.username, "password": self.password})
            self.logged_in = status == 200
            self.last_error = None if self.logged_in else f"login failed ({status}): {payload.get('error') if isinstance(payload, dict) else payload}"
            return self.logged_in

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Any = None,
                headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any, Dict[str, str]]:
        """Authenticated request; logs in on demand and once more after a 401."""
        if not self.logged_in and not self.login():
            raise GraywolfError(self.last_error or "not logged in")
        status, payload, hdrs = self._raw(method, path, params, body, headers)
        if status == 401:
            self.logged_in = False
            if self.login():
                status, payload, hdrs = self._raw(method, path, params, body, headers)
        if status >= 400:
            msg = payload.get("error") if isinstance(payload, dict) else payload
            self.last_error = f"{method} {path} -> {status}: {msg}"
            raise GraywolfError(self.last_error)
        self.last_error = None
        return status, payload, hdrs

    # -- endpoints ---------------------------------------------------------------
    def get_version(self) -> Optional[str]:
        status, payload, _ = self._raw("GET", "/version")
        if status == 200 and isinstance(payload, dict):
            self.version = payload.get("version") or payload.get("Version") or json.dumps(payload)
        elif status == 200 and isinstance(payload, str):
            self.version = payload.strip()
        return self.version

    def stations(self, bbox: str = WORLD_BBOX, timerange: int = 3600, since: Optional[str] = None,
                 etag: Optional[str] = None) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """-> (stations or None if 304 Not Modified, new etag)."""
        hdrs = {"If-None-Match": etag} if etag else None
        status, payload, rh = self.request("GET", "/stations", {"bbox": bbox, "timerange": timerange, "since": since}, headers=hdrs)
        if status == 304:
            return None, etag
        return (payload if isinstance(payload, list) else []), rh.get("etag")

    def position(self) -> Dict[str, Any]:
        _, payload, _ = self.request("GET", "/position")
        return payload if isinstance(payload, dict) else {}

    def test(self) -> Dict[str, Any]:
        """Login + version + a station count; never raises."""
        out: Dict[str, Any] = {"ok": False, "base": self.base}
        try:
            out["version"] = self.get_version()
            if not self.login():
                out["error"] = self.last_error
                return out
            sts, _ = self.stations(timerange=86400)
            out["stations_24h"] = len(sts or [])
            pos = self.position()
            out["position"] = {k: pos.get(k) for k in ("valid", "source", "lat", "lon", "speed_kt", "heading_deg")}
            out["ok"] = True
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
        return out


# -- mapping -------------------------------------------------------------------------

def _first_position(st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pos = st.get("positions") or []
    if isinstance(pos, list) and pos:
        p = pos[0]
        if isinstance(p, dict):
            return p
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            return {"lat": p[0], "lon": p[1]}
    if st.get("lat") is not None and st.get("lon") is not None:
        return {"lat": st["lat"], "lon": st["lon"]}
    return None


def station_to_observation(st: Dict[str, Any], ident: str = "gw") -> Optional[Observation]:
    """Graywolf StationDTO -> Observation (source 'aprs').  Position-less stations
    still count as heard.  Objects/items keep their object name as the callsign."""
    raw_call = (st.get("callsign") or "").strip()
    if not raw_call:
        return None
    callsign, _ = normalise_callsign(raw_call)
    heard = parse_rfc3339(st.get("last_heard")) or now_utc()
    p = _first_position(st)
    lat = lon = None
    if p:
        try:
            lat, lon = float(p.get("lat", p.get("latitude"))), float(p.get("lon", p.get("longitude")))
        except (TypeError, ValueError):
            lat = lon = None
        if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
            lat = lon = None
        if lat is not None and abs(lat) < 1e-6 and abs(lon) < 1e-6:
            lat = lon = None
    symbol = None
    if st.get("symbol_table") or st.get("symbol_code"):
        symbol = f"{st.get('symbol_table') or '/'}{st.get('symbol_code') or ''}"
    comment = (st.get("comment") or "").strip() or None
    return Observation(
        callsign=callsign, heard_at=heard, source="aprs",
        source_ref=f"{ident}|aprs:{callsign}:{heard.strftime('%Y%m%dT%H%M%S.%f')}",
        frame_kind="aprs", grid=latlon_to_grid(lat, lon) if lat is not None else None,
        lat=lat, lon=lon, accuracy_m=(20.0 if lat is not None else None),
        band="APRS", text=comment, symbol=symbol, is_object=bool(st.get("is_object")),
        raw={"via": st.get("via"), "path": st.get("path"), "hops": st.get("hops"), "gated": st.get("gated"),
             "direction": st.get("direction"), "originator": st.get("source"), "channel": st.get("channel"),
             "speed_kt": (p or {}).get("speed_kt"), "course": (p or {}).get("course"),
             "alt_m": (p or {}).get("alt_m") if (p or {}).get("has_alt") else None},
    )


def position_to_own_fix(pos: Dict[str, Any]) -> Optional[OwnFix]:
    """Graywolf PositionDTO -> OwnFix (source 'graywolf').  None if no valid position."""
    if not pos or not pos.get("valid"):
        return None
    try:
        lat, lon = float(pos["lat"]), float(pos["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return None
    src = (pos.get("source") or "").lower()
    if src == "none":
        return None
    t = parse_rfc3339(pos.get("timestamp")) if src == "gps" else None
    speed = float(pos["speed_kt"]) * KNOTS_TO_KMH if pos.get("speed_kt") is not None and src == "gps" else None
    course = float(pos["heading_deg"]) if pos.get("has_course") and pos.get("heading_deg") is not None else None
    alt = float(pos["alt_m"]) if pos.get("has_alt") and pos.get("alt_m") is not None else None
    return OwnFix(lat=lat, lon=lon, time=t or now_utc(), source="graywolf", grid=latlon_to_grid(lat, lon),
                  speed_kmh=speed, course_deg=course, altitude_m=alt, accuracy_m=10.0,
                  fix_quality="gps" if src == "gps" else "fixed")
