"""When to transmit our own position.  Pure policy: consumes timed fixes,
emits decisions.  It never knows HOW a beacon is sent.

Two policies share the same safety envelope (hard floor, keepalive):

FixedIntervalPolicy  - every N minutes, optionally only if we moved.
SmartBeaconPolicy    - APRS-style SmartBeaconing adapted for VarAC:
    * speed-dependent interval between slow_rate and fast_rate
    * corner pegging on heading change (turn_min + turn_slope / speed)
    * grid-change trigger with dwell/edge hysteresis (the only event that
      changes an ADVANCED-BEACON payload, and a natural trigger for a
      broadcast too)
    * minimum-move guard so a stationary station does not repeat itself
    * keepalive at max_interval so silence still means something

The policy needs no speed or course to work: without them it degrades to
"grid change / moved far enough / keepalive", which is most of the value.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .geo import angle_diff_deg, haversine_km
from .grid import distance_inside_cell_m, latlon_to_grid

HARD_FLOOR_SECONDS = 60  # absolute; no configuration can go below this


@dataclass(frozen=True)
class Fix:
    lat: float
    lon: float
    time: datetime
    speed_kmh: Optional[float] = None
    course_deg: Optional[float] = None

    @property
    def grid(self) -> Optional[str]:
        return latlon_to_grid(self.lat, self.lon, 6)


@dataclass(frozen=True)
class Decision:
    send: bool
    reason: str                       # why we send, or why not
    next_due_seconds: Optional[float] = None   # best estimate until the next possible send


@dataclass
class _Pending:
    grid: str
    since: datetime


DEFAULT_SMART = {
    "min_interval_seconds": 300,
    "max_interval_seconds": 1800,
    "slow_speed_kmh": 5.0,
    "slow_rate_seconds": 1800,
    "fast_speed_kmh": 90.0,
    "fast_rate_seconds": 300,
    "min_turn_time_seconds": 60,
    "turn_min_deg": 30.0,
    "turn_slope": 255.0,
    "min_move_m": 500.0,
    "grid_change_triggers": True,
    "grid_dwell_seconds": 90,
    "grid_edge_margin_m": 300.0,
}

DEFAULT_FIXED = {
    "interval_seconds": 900,
    "only_if_moved": False,
    "min_move_m": 500.0,
    "max_interval_seconds": 3600,   # keepalive when only_if_moved
}


def _floor(cfg_min: float) -> float:
    return max(float(cfg_min), HARD_FLOOR_SECONDS)


class _BasePolicy:
    def __init__(self) -> None:
        self.last_tx: Optional[Fix] = None
        self.last_tx_grid: Optional[str] = None

    def mark_sent(self, fix: Fix, when: Optional[datetime] = None) -> None:
        self.last_tx = Fix(fix.lat, fix.lon, when or fix.time, fix.speed_kmh, fix.course_deg)
        self.last_tx_grid = fix.grid

    def arm(self, fix: Fix, now: datetime) -> None:
        """Start the clock without transmitting: the first beacon comes one
        interval after enabling (or sooner if smart rules trigger)."""
        self.mark_sent(fix, now)

    @property
    def armed(self) -> bool:
        return self.last_tx is not None

    def reset(self) -> None:
        self.last_tx = None
        self.last_tx_grid = None

    def _moved_m(self, fix: Fix) -> float:
        if self.last_tx is None:
            return float("inf")
        return haversine_km(self.last_tx.lat, self.last_tx.lon, fix.lat, fix.lon) * 1000.0

    def evaluate(self, fix: Fix, now: datetime) -> Decision:  # pragma: no cover - abstract
        raise NotImplementedError


class FixedIntervalPolicy(_BasePolicy):
    def __init__(self, cfg: Optional[dict] = None) -> None:
        super().__init__()
        self.cfg = {**DEFAULT_FIXED, **(cfg or {})}

    @property
    def interval(self) -> float:
        return _floor(self.cfg["interval_seconds"])

    def evaluate(self, fix: Fix, now: datetime) -> Decision:
        if self.last_tx is None:
            self.arm(fix, now)
            return Decision(False, "armed", self.interval)
        since = (now - self.last_tx.time).total_seconds()
        if since < self.interval:
            return Decision(False, "interval", self.interval - since)
        if self.cfg.get("only_if_moved"):
            moved = self._moved_m(fix)
            if moved < float(self.cfg["min_move_m"]):
                keep = float(self.cfg["max_interval_seconds"])
                if keep <= 0:                       # 0 = no keepalive: a parked station stays silent
                    return Decision(False, "not_moved", None)
                keep = max(self.interval, keep)
                if since >= keep:
                    return Decision(True, "keepalive")
                return Decision(False, "not_moved", keep - since)
            return Decision(True, "moved")
        return Decision(True, "fixed")


class SmartBeaconPolicy(_BasePolicy):
    def __init__(self, cfg: Optional[dict] = None) -> None:
        super().__init__()
        self.cfg = {**DEFAULT_SMART, **(cfg or {})}
        self._pending: Optional[_Pending] = None

    def reset(self) -> None:
        super().reset()
        self._pending = None

    # -- helpers ---------------------------------------------------------
    @property
    def min_interval(self) -> float:
        return _floor(self.cfg["min_interval_seconds"])

    @property
    def max_interval(self) -> float:
        mi = float(self.cfg["max_interval_seconds"])
        if mi <= 0:                                 # 0 = no keepalive
            return float("inf")
        return max(mi, self.min_interval)

    def rate_for_speed(self, speed_kmh: Optional[float]) -> float:
        """Classic SmartBeaconing interval, clamped into [min, max]."""
        c = self.cfg
        if speed_kmh is None or speed_kmh <= float(c["slow_speed_kmh"]):
            rate = float(c["slow_rate_seconds"])
        elif speed_kmh >= float(c["fast_speed_kmh"]):
            rate = float(c["fast_rate_seconds"])
        else:
            rate = float(c["fast_rate_seconds"]) * float(c["fast_speed_kmh"]) / speed_kmh
        return min(max(rate, self.min_interval), self.max_interval)

    def grid_changed_stably(self, fix: Fix, now: datetime) -> bool:
        """True only once a NEW grid has been held for the dwell time and the
        fix is at least edge_margin inside it.  Kills boundary chatter."""
        g = fix.grid
        if not g or g == self.last_tx_grid:
            self._pending = None
            return False
        if self._pending is None or self._pending.grid != g:
            self._pending = _Pending(grid=g, since=now)
            return False
        if (now - self._pending.since).total_seconds() < float(self.cfg["grid_dwell_seconds"]):
            return False
        inside = distance_inside_cell_m(fix.lat, fix.lon, g)
        return inside is not None and inside >= float(self.cfg["grid_edge_margin_m"])

    # -- the decision ----------------------------------------------------
    def evaluate(self, fix: Fix, now: datetime) -> Decision:
        if self.last_tx is None:
            self.arm(fix, now)
            return Decision(False, "armed", self.rate_for_speed(fix.speed_kmh))
        since = (now - self.last_tx.time).total_seconds()
        # Track pending grid transitions even while floored, so dwell accrues.
        grid_ok = self.grid_changed_stably(fix, now)
        if since < self.min_interval:
            return Decision(False, "floor", self.min_interval - since)
        if since >= self.max_interval:
            return Decision(True, "keepalive")
        if self.cfg.get("grid_change_triggers") and grid_ok:
            return Decision(True, "grid_change")
        moved = self._moved_m(fix)
        min_move = float(self.cfg["min_move_m"])
        rate = self.rate_for_speed(fix.speed_kmh)
        speed = fix.speed_kmh
        # Corner pegging needs speed AND course on both ends.
        if (speed is not None and fix.course_deg is not None and self.last_tx.course_deg is not None
                and speed > float(self.cfg["slow_speed_kmh"])
                and since >= float(self.cfg["min_turn_time_seconds"])
                and moved >= min_move):
            turn = angle_diff_deg(fix.course_deg, self.last_tx.course_deg)
            threshold = min(120.0, float(self.cfg["turn_min_deg"]) + float(self.cfg["turn_slope"]) / max(speed, 1.0))
            if turn >= threshold:
                return Decision(True, "turn")
        if since >= rate:
            if moved >= min_move:
                return Decision(True, "rate")
            return Decision(False, "not_moved", None if self.max_interval == float("inf") else self.max_interval - since)
        return Decision(False, "rate", rate - since)


def make_policy(mode: str, cfg: Optional[dict]) -> _BasePolicy:
    if (mode or "fixed").lower() == "smart":
        return SmartBeaconPolicy(cfg)
    return FixedIntervalPolicy(cfg)
