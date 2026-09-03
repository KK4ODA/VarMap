"""JSON configuration with per-section defaults merge (HamLink's pattern):
new keys appear on upgrade without wiping the user's settings.

Layout:
    <app dir>/config.json                 portable, beside the app
    %LOCALAPPDATA%/VarMap/varmap.db       our station database (never inside
    %LOCALAPPDATA%/VarMap/tiles.mbtiles   Dropbox: SQLite + a sync client = corruption)
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from typing import Any, Dict, Optional

APP_NAME = "VarMap"

DEFAULT_CONFIG: Dict[str, Any] = {
    "data_dir": "",                       # '' => %LOCALAPPDATA%\VarMap
    "varac": {
        "db_path": "",                    # '' => discover from exe/profile, then C:\VarAC\VarAC.db
        "exe_path": "",
        "profile": "",                    # '' => VarAC.ini
        "poll_interval_seconds": 10,
        "batch_size": 2000,
        "read_cq_frames": True,
        "scan_broadcasts": True,
        "scan_vmail_gps_tags": True,
        "backfill": "all",                # all | days:<n> | none   (first run only)
    },
    "own_station": {
        "callsign": "",                   # '' => Mycall from VarAC.ini
        "position_source": "auto",        # auto | graywolf | gps_log | manual_ini | my_locator | nmea
        "gps_log_path": "",               # '' => WriteGPSDataToFileName from VarAC.ini
        "nmea_com_port": "",
        "nmea_baud": 9600,
        "update_interval_seconds": 5,
        "record_interval_seconds": 60,    # store a fix at least this often even if unmoved
        "record_min_move_m": 25.0,
    },
    "graywolf": {                         # APRS via the Graywolf station software (receive-only for now)
        "enabled": False,
        "url": "http://127.0.0.1:8080",
        "username": "",
        "password": "",
        "poll_interval_seconds": 10,
        "lookback_seconds": 3600,         # per delta poll
        "backfill_seconds": 86400,        # first poll after enabling
        "bbox": "",                       # '' = whole world; else sw_lat,sw_lon,ne_lat,ne_lon
        "use_for_own_position": True,     # offer Graywolf's GPS fix in the own-position ladder
    },
    "staleness": {"fresh_minutes": 30, "recent_hours": 2, "stale_hours": 24, "hide_after_days": 30},
    "history": {"keep_days": 90, "max_positions_per_station": 1000, "prune_interval_minutes": 60},
    "map": {"default_zoom": 5, "show_grid_squares": True, "units": "auto"},   # auto => VarAC's LocatorsDistanceUnit
    "tiles": {
        "source_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors",
        "online_fetch": True,             # fetch + cache tiles the browser asks for
        "max_zoom": 17,
        "download_max_tiles": 20000,      # per region job; OSM policy discourages bulk downloads
        "download_concurrency": 2,
        "download_delay_ms": 50,
        "user_agent": "VarMap/0.1 (VarAC companion; amateur radio mapping)",
    },
    "beacon": {
        "enabled": False,                 # master switch; default OFF
        "dry_run": True,                  # log what WOULD be sent, transmit nothing
        "mode": "fixed",                  # fixed | smart
        "method": "broadcast",            # broadcast | beacon   (beacon = one-time advanced beacon, experimental)
        "broadcast_to": "ALL",
        "message_template": "{gpstag} {grid} {comment}",
        "comment": "VarMap",
        "coord_decimals": 5,              # 5 = ~1 m; 3 = ~100 m; 2 = ~1 km (privacy)
        "dcd_guard": True,                # refuse to hand VarAC a broadcast while its 'Ignore DCD' box is ticked
        "max_fix_age_seconds": 900,       # never beacon a stale position
        "max_per_hour": 2,                # independent rate limiter (hard cap 6, see services/beacon.py)
        "cf_window_hz": 3000,             # +- window around a calling frequency that counts as "on it" (covers VarAC's slots)
        "fixed": {"interval_seconds": 1800, "only_if_moved": True, "min_move_m": 500.0, "max_interval_seconds": 3600},
        # Smart timing defaults follow HF APRS practice (VARA HF trackers): ~10 min while moving,
        # 60 min stationary, no corner pegging faster than the 10-minute floor.  See the VHF profile in the UI.
        "smart": {
            "profile": "hf",
            "min_interval_seconds": 600, "max_interval_seconds": 3600,
            "slow_speed_kmh": 5.0, "slow_rate_seconds": 3600,
            "fast_speed_kmh": 90.0, "fast_rate_seconds": 600,
            "min_turn_time_seconds": 600, "turn_min_deg": 30.0, "turn_slope": 255.0,
            "min_move_m": 1000.0, "grid_change_triggers": True,
            "grid_dwell_seconds": 120, "grid_edge_margin_m": 300.0,
        },
    },
    "web": {"host": "127.0.0.1", "port": 5001, "open_browser": True},
    "privacy": {"share_own_position_externally": False},
}


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def default_config_path() -> str:
    """Portable when running from source or when a config.json already sits beside
    the executable; otherwise (installed build under Program Files / Applications)
    the config lives with the data in the user profile."""
    beside = os.path.join(app_dir(), "config.json")
    if not getattr(sys, "frozen", False) or os.path.isfile(beside):
        return beside
    d = default_data_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config.json")


def deep_merge(defaults: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(defaults)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """Thread-safe config holder.  `snapshot()` returns a deep copy; never hand
    out the live dict."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or default_config_path()
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        with self._lock:
            if os.path.isfile(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        saved = json.load(f)
                    self._data = deep_merge(DEFAULT_CONFIG, saved)
                    return
                except Exception as e:  # corrupt config: keep defaults, do not overwrite
                    print(f"[CONFIG] Could not read {self.path}: {e} - using defaults")
            self._data = copy.deepcopy(DEFAULT_CONFIG)

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self.path)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, *keys: str, default: Any = None) -> Any:
        with self._lock:
            cur: Any = self._data
            for k in keys:
                if not isinstance(cur, dict) or k not in cur:
                    return default
                cur = cur[k]
            return copy.deepcopy(cur)

    def update(self, patch: Dict[str, Any], save: bool = True) -> None:
        with self._lock:
            self._data = deep_merge(self._data, patch)
            if save:
                self.save()

    def set(self, value: Any, *keys: str, save: bool = True) -> None:
        patch: Any = value
        for k in reversed(keys):
            patch = {k: patch}
        self.update(patch, save=save)

    # -- derived paths -----------------------------------------------------
    def data_dir(self) -> str:
        d = self.get("data_dir") or default_data_dir()
        os.makedirs(d, exist_ok=True)
        return d

    def db_path(self) -> str:
        return os.path.join(self.data_dir(), "varmap.db")

    def tiles_path(self) -> str:
        return os.path.join(self.data_dir(), "tiles.mbtiles")

    def log_path(self) -> str:
        return os.path.join(self.data_dir(), "varmap.log")
