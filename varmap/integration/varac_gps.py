"""Our own position, sourced from VarAC wherever possible.

Priority (design doc 3.6):
1. VarAC_gps.log   - when [GPS] WriteGPSDataToFile=ON.  VarAC documents this
                     file as "for analysis with geo applications".  Live, no
                     COM-port contention.  Format is NOT yet verified: this
                     reader accepts raw NMEA, 'lat,lon' lines, timestamped
                     CSV, and <GPS:...> style text, and takes the LAST
                     parseable line.
2. NMEA serial     - our own reader, ONLY when VarAC's GPS is off or the user
                     has a second receiver (a COM port cannot be shared).
3. ManualGPSData   - [GPS] ManualGPSData in decimal degrees.  Exact, static.
4. MyLocator       - grid centroid, +-2.3 km.  Always available.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..domain.gpstag import parse_coordinate_text, parse_position_text
from ..domain.grid import grid_to_latlon, latlon_to_grid, normalise_grid
from ..domain.timeparse import now_utc, parse_varac_time
from .contracts import OwnFix

log = logging.getLogger("varmap.gps")

KNOTS_TO_KMH = 1.852
_NMEA_RE = re.compile(r"\$(?:GP|GN|GL|GA|GB)(RMC|GGA),([^*]*)\*?([0-9A-Fa-f]{2})?")


def _nmea_deg(value: str, hemi: str) -> Optional[float]:
    """'3350.338,N' style -> decimal degrees."""
    if not value or not hemi:
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    deg = int(f // 100)
    mins = f - deg * 100
    d = deg + mins / 60.0
    if hemi.upper() in ("S", "W"):
        d = -d
    return d


def parse_nmea_sentence(line: str, ref_date: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """RMC / GGA -> dict(lat, lon, time, speed_kmh, course_deg, altitude_m, quality).
    Returns None for other sentences or invalid fixes."""
    m = _NMEA_RE.search(line or "")
    if not m:
        return None
    kind, body = m.group(1), m.group(2)
    f = body.split(",")
    try:
        if kind == "RMC":
            # time,status,lat,N,lon,E,speed_kn,course,date,...
            if len(f) < 9 or f[1].upper() != "A":
                return None
            lat, lon = _nmea_deg(f[2], f[3]), _nmea_deg(f[4], f[5])
            if lat is None or lon is None:
                return None
            speed = float(f[6]) * KNOTS_TO_KMH if f[6] else None
            course = float(f[7]) if f[7] else None
            t = _nmea_time(f[0], f[8])
            return {"lat": lat, "lon": lon, "time": t, "speed_kmh": speed, "course_deg": course,
                    "altitude_m": None, "quality": "A"}
        if kind == "GGA":
            # time,lat,N,lon,E,quality,nsat,hdop,alt,M,...
            if len(f) < 9 or f[5] in ("", "0"):
                return None
            lat, lon = _nmea_deg(f[1], f[2]), _nmea_deg(f[3], f[4])
            if lat is None or lon is None:
                return None
            alt = float(f[8]) if f[8] else None
            t = _nmea_time(f[0], None, ref_date)
            return {"lat": lat, "lon": lon, "time": t, "speed_kmh": None, "course_deg": None,
                    "altitude_m": alt, "quality": f[5]}
    except (ValueError, IndexError):
        return None
    return None


def _nmea_time(hhmmss: str, ddmmyy: Optional[str], ref_date: Optional[datetime] = None) -> Optional[datetime]:
    if not hhmmss or len(hhmmss) < 6:
        return None
    try:
        h, mi, s = int(hhmmss[0:2]), int(hhmmss[2:4]), float(hhmmss[4:])
        if ddmmyy and len(ddmmyy) >= 6:
            yy = int(ddmmyy[4:6])
            d, mo, y = int(ddmmyy[0:2]), int(ddmmyy[2:4]), (1900 if yy >= 80 else 2000) + yy
        else:
            base = ref_date or now_utc()
            d, mo, y = base.day, base.month, base.year
        return datetime(y, mo, d, h, mi, int(s), int((s - int(s)) * 1e6), tzinfo=timezone.utc)
    except ValueError:
        return None


# VarAC V15.0.18's actual WriteGPSDataToFile format (verified 2026-09-02), one line per second:
#   2026-09-02 22:09:10,Lat: 33°51.6000 N Long: 084°18.0000 W \r\n
# Timestamp is UTC.  With no fix the values are empty ('Lat: °  Long: °').  No speed/course.
_VARAC_GPS_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s*,\s*"
    r"Lat(?:itude)?\s*:\s*(?P<lat>[^,]*?)\s*Long(?:itude)?\s*:\s*(?P<lon>.*?)\s*$", re.I)


def parse_varac_gps_line(line: str) -> Optional[Dict[str, Any]]:
    """VarAC's own 'Lat: … Long: …' log line -> fix dict, or None (no fix / other format)."""
    m = _VARAC_GPS_LINE.match((line or "").strip())
    if not m:
        return None
    # Strip the degree sign (and any decoding artefact of it) around the numbers.
    lat_s = re.sub(r"[°�]", " ", m.group("lat")).strip()
    lon_s = re.sub(r"[°�]", " ", m.group("lon")).strip()
    if not lat_s or not lon_s:
        return None  # GPS has no fix yet
    coords = None
    num = r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*$"
    ma, mo = re.match(num, lat_s), re.match(num, lon_s)
    if ma and mo:
        coords = (float(ma.group(1).replace(",", ".")), float(mo.group(1).replace(",", ".")))
        if not (-90.0 <= coords[0] <= 90.0 and -180.0 <= coords[1] <= 180.0):
            coords = None
    else:
        coords = parse_coordinate_text(f"{lat_s} {lon_s}")   # DMS / hemisphere variants
    if coords is None or (abs(coords[0]) < 1e-6 and abs(coords[1]) < 1e-6):
        return None
    return {"lat": coords[0], "lon": coords[1], "time": parse_varac_time(m.group("ts")), "speed_kmh": None,
            "course_deg": None, "altitude_m": None, "quality": None}


def parse_gps_log_line(line: str, ref_date: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """One line of VarAC_gps.log in any of the formats we anticipate."""
    s = (line or "").strip()
    if not s:
        return None
    if s.startswith("$"):
        return parse_nmea_sentence(s, ref_date)
    if _VARAC_GPS_LINE.match(s):
        return parse_varac_gps_line(s)
    # timestamped CSV: '2026-09-02 01:00:00,33.86000,-84.30000[,speed,course]'
    parts = [p.strip() for p in re.split(r"[,;\t]", s)]
    t: Optional[datetime] = None
    if parts and parse_varac_time(parts[0]) is not None:
        t = parse_varac_time(parts[0])
        parts = parts[1:]
    coords = None
    if len(parts) >= 2:
        coords = parse_coordinate_text(parts[0] + "," + parts[1])
    if coords is None:
        pos = parse_position_text(s)
        if pos and pos.kind == "gps":
            coords = (pos.lat, pos.lon)
        else:
            coords = parse_coordinate_text(s)
    if coords is None:
        return None
    speed = course = None
    if len(parts) >= 3:
        try:
            speed = float(parts[2])
        except ValueError:
            speed = None
    if len(parts) >= 4:
        try:
            course = float(parts[3])
        except ValueError:
            course = None
    return {"lat": coords[0], "lon": coords[1], "time": t, "speed_kmh": speed, "course_deg": course,
            "altitude_m": None, "quality": None}


def read_gps_log(path: str, tail_bytes: int = 65536) -> Optional[Dict[str, Any]]:
    """Latest parseable fix in the file, with the file mtime as fallback time."""
    try:
        size = os.path.getsize(path)
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            data = f.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    best: Optional[Dict[str, Any]] = None
    # Walk backwards; merge an RMC (speed/course) with a nearby GGA (altitude).
    for line in reversed(lines[-500:]):
        fix = parse_gps_log_line(line, mtime)
        if not fix:
            continue
        if best is None:
            best = fix
            if fix.get("speed_kmh") is not None or not line.startswith("$"):
                break
        else:
            if best.get("altitude_m") is None and fix.get("altitude_m") is not None:
                best["altitude_m"] = fix["altitude_m"]
            if best.get("speed_kmh") is None and fix.get("speed_kmh") is not None:
                best["speed_kmh"], best["course_deg"] = fix["speed_kmh"], fix["course_deg"]
            break
    if best is None:
        return None
    if best.get("time") is None:
        best["time"] = mtime
    best["file_mtime"] = mtime
    return best


class NmeaSerialReader:
    """Background reader for a directly attached GPS (opt-in).  Lazy-imports
    pyserial so it is not a hard dependency."""

    def __init__(self, port: str, baud: int = 9600) -> None:
        self.port, self.baud = port, int(baud)
        self._lock = threading.Lock()
        self._latest: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="varmap-nmea", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._latest) if self._latest else None

    def _run(self) -> None:
        try:
            import serial  # type: ignore
        except ImportError:
            self.error = "pyserial not installed (pip install pyserial)"
            log.warning(self.error)
            return
        while not self._stop.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=2) as ser:
                    self.error = None
                    log.info("NMEA reader open on %s @ %d", self.port, self.baud)
                    while not self._stop.is_set():
                        raw = ser.readline()
                        if not raw:
                            continue
                        line = raw.decode("ascii", errors="ignore").strip()
                        fix = parse_nmea_sentence(line)
                        if fix:
                            with self._lock:
                                if self._latest and fix.get("speed_kmh") is None:
                                    fix["speed_kmh"] = self._latest.get("speed_kmh")
                                    fix["course_deg"] = self._latest.get("course_deg")
                                if self._latest and fix.get("altitude_m") is None:
                                    fix["altitude_m"] = self._latest.get("altitude_m")
                                fix["received"] = now_utc()
                                self._latest = fix
            except Exception as e:  # port missing, in use by VarAC, unplugged...
                self.error = str(e)
                log.warning("NMEA reader error on %s: %s (retry in 30 s)", self.port, e)
                self._stop.wait(30)


class OwnPositionReader:
    """Resolves our own position from the configured or best available source."""

    def __init__(self, varac_config, cfg) -> None:
        self.vc = varac_config
        self.cfg = cfg
        self._nmea: Optional[NmeaSerialReader] = None
        self.last_source: Optional[str] = None
        self.last_error: Optional[str] = None
        self.warnings: List[str] = []

    # -- individual sources ----------------------------------------------
    def from_gps_log(self) -> Optional[OwnFix]:
        path = self.vc.gps_log_path()
        if not path or not os.path.isfile(path):
            return None
        fix = read_gps_log(path)
        if not fix:
            return None
        return OwnFix(lat=fix["lat"], lon=fix["lon"], time=fix["time"], source="gps_log",
                      grid=latlon_to_grid(fix["lat"], fix["lon"]), speed_kmh=fix.get("speed_kmh"),
                      course_deg=fix.get("course_deg"), altitude_m=fix.get("altitude_m"),
                      accuracy_m=10.0, fix_quality=fix.get("quality"))

    def from_nmea(self) -> Optional[OwnFix]:
        port = (self.cfg.get("own_station", "nmea_com_port") or "").strip()
        if not port:
            if self._nmea:
                self._nmea.stop()
                self._nmea = None
            return None
        g = self.vc.gps_settings()
        if g.get("enabled") and (g.get("com_port") or "").upper() == port.upper():
            w = f"NMEA port {port} is also VarAC's GPS port; a COM port cannot be shared. Not opening it."
            if w not in self.warnings:
                self.warnings.append(w)
            return None
        baud = int(self.cfg.get("own_station", "nmea_baud") or 9600)
        if self._nmea is None or self._nmea.port != port or self._nmea.baud != baud:
            if self._nmea:
                self._nmea.stop()
            self._nmea = NmeaSerialReader(port, baud)
            self._nmea.start()
        fix = self._nmea.latest()
        if not fix:
            return None
        return OwnFix(lat=fix["lat"], lon=fix["lon"], time=fix.get("time") or fix.get("received") or now_utc(),
                      source="nmea", grid=latlon_to_grid(fix["lat"], fix["lon"]), speed_kmh=fix.get("speed_kmh"),
                      course_deg=fix.get("course_deg"), altitude_m=fix.get("altitude_m"), accuracy_m=10.0,
                      fix_quality=fix.get("quality"))

    def from_manual_ini(self) -> Optional[OwnFix]:
        g = self.vc.gps_settings()
        raw = g.get("manual_data") or ""
        if not raw:
            return None
        c = parse_coordinate_text(raw)
        if not c:
            return None
        # Static sources are "current" by definition: the operator entered them deliberately.
        return OwnFix(lat=c[0], lon=c[1], time=now_utc(), source="manual_ini", grid=latlon_to_grid(c[0], c[1]),
                      accuracy_m=10.0)

    def from_my_locator(self) -> Optional[OwnFix]:
        g = normalise_grid(self.vc.my_locator())
        c = grid_to_latlon(g)
        if not c:
            return None
        return OwnFix(lat=c[0], lon=c[1], time=now_utc(), source="my_locator", grid=g, accuracy_m=3800.0)

    # -- resolution ------------------------------------------------------
    def read(self) -> Optional[OwnFix]:
        mode = (self.cfg.get("own_station", "position_source") or "auto").lower()
        ladder = {
            "gps_log": [self.from_gps_log],
            "nmea": [self.from_nmea],
            "manual_ini": [self.from_manual_ini],
            "my_locator": [self.from_my_locator],
        }.get(mode, [self.from_gps_log, self.from_nmea, self.from_manual_ini, self.from_my_locator])
        for fn in ladder:
            try:
                fix = fn()
            except Exception as e:
                self.last_error = f"{fn.__name__}: {e}"
                log.debug("own position source %s failed: %s", fn.__name__, e)
                continue
            if fix:
                self.last_source = fix.source
                return fix
        self.last_source = None
        return None

    def describe(self) -> Dict[str, Any]:
        return {
            "mode": self.cfg.get("own_station", "position_source"),
            "last_source": self.last_source,
            "last_error": self.last_error,
            "gps_log_path": self.vc.gps_log_path(),
            "gps_log_exists": bool(self.vc.gps_log_path() and os.path.isfile(self.vc.gps_log_path())),
            "nmea_port": self.cfg.get("own_station", "nmea_com_port"),
            "nmea_error": self._nmea.error if self._nmea else None,
            "warnings": list(self.warnings),
        }
