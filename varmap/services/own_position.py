"""Keeps our own position current, derives speed/course when the source has
none, and records a track in our database."""
from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Any, Dict, Optional

from ..domain.geo import bearing_deg, haversine_km
from ..domain.timeparse import iso_utc, now_utc
from ..integration.contracts import OwnFix
from ..integration.varac_gps import OwnPositionReader

log = logging.getLogger("varmap.own")
DYNAMIC_SOURCES = ("gps_log", "nmea")


class OwnPositionTracker(threading.Thread):
    def __init__(self, rt) -> None:
        super().__init__(name="varmap-own", daemon=True)
        self.rt = rt
        self.cfg = rt.cfg
        self.repo = rt.repo
        self.reader = OwnPositionReader(rt.vc, rt.cfg)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._current: Optional[OwnFix] = None
        self._prev_raw: Optional[OwnFix] = None
        self._speed_ema: Optional[float] = None
        self._course: Optional[float] = None
        self._last_record: Optional[OwnFix] = None
        self._last_record_wall = 0.0

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def current(self) -> Optional[OwnFix]:
        with self._lock:
            return self._current

    def run(self) -> None:
        log.info("own-position tracker started")
        while not self._stop.is_set():
            try:
                fix = self.reader.read()
                if fix:
                    fix = self._derive(fix)
                    with self._lock:
                        self._current = fix
                    self._maybe_record(fix)
                else:
                    with self._lock:
                        self._current = None
            except Exception as e:  # pragma: no cover
                log.exception("own position update failed: %s", e)
            iv = max(1, int(self.cfg.get("own_station", "update_interval_seconds") or 5))
            self._wake.wait(iv)
            self._wake.clear()

    def _derive(self, fix: OwnFix) -> OwnFix:
        """Fill in speed/course from successive fixes when the source lacks them."""
        prev = self._prev_raw
        if fix.source not in DYNAMIC_SOURCES:
            self._prev_raw = fix
            self._speed_ema, self._course = None, None
            return fix
        if fix.speed_kmh is not None:
            self._prev_raw = fix
            self._speed_ema, self._course = fix.speed_kmh, fix.course_deg
            return fix
        if prev is None or prev.source != fix.source:
            self._prev_raw = fix
            return fix
        if fix.time <= prev.time:
            # Same fix as before (file not updated): keep the previously derived values.
            return dataclasses.replace(fix, speed_kmh=self._speed_ema, course_deg=self._course)
        dt = (fix.time - prev.time).total_seconds()
        dist_m = haversine_km(prev.lat, prev.lon, fix.lat, fix.lon) * 1000.0
        if dt >= 3.0:
            # A stationary receiver jitters by a few metres; below ~8 m per interval call it 0.
            speed = 0.0 if dist_m < 8.0 else dist_m / dt * 3.6
            self._speed_ema = speed if self._speed_ema is None else 0.5 * speed + 0.5 * self._speed_ema
            if self._speed_ema < 1.0:
                self._speed_ema = 0.0
            if dist_m >= 15.0:
                self._course = bearing_deg(prev.lat, prev.lon, fix.lat, fix.lon)
            self._prev_raw = fix
        return dataclasses.replace(fix, speed_kmh=self._speed_ema, course_deg=self._course)

    def _maybe_record(self, fix: OwnFix) -> None:
        import time
        last = self._last_record
        min_move = float(self.cfg.get("own_station", "record_min_move_m") or 25.0)
        interval = float(self.cfg.get("own_station", "record_interval_seconds") or 60)
        moved = float("inf") if last is None else haversine_km(last.lat, last.lon, fix.lat, fix.lon) * 1000.0
        due = time.time() - self._last_record_wall >= interval
        changed_source = last is None or last.source != fix.source
        if changed_source or moved >= min_move or (due and fix.source in DYNAMIC_SOURCES):
            try:
                self.repo.own_position_add(fix)
                self._last_record = fix
                self._last_record_wall = time.time()
            except Exception as e:
                log.debug("own position record failed: %s", e)

    def describe(self) -> Dict[str, Any]:
        fix = self.current()
        d = self.reader.describe()
        d["fix"] = None
        if fix:
            d["fix"] = {
                "lat": fix.lat, "lon": fix.lon, "grid": fix.grid, "source": fix.source,
                "time": iso_utc(fix.time), "age_seconds": (now_utc() - fix.time).total_seconds(),
                "speed_kmh": fix.speed_kmh, "course_deg": fix.course_deg, "altitude_m": fix.altitude_m,
                "accuracy_m": fix.accuracy_m, "quality": fix.fix_quality,
            }
        return d
