"""Everything that crosses the integration boundary.  No VarAC concepts leak
upward: no cqframe, no folder_id, no instance_id semantics."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Observation:
    """One observation of one station at one instant.

    Position may be absent (a standard beacon carries no locator): the event
    still updates last-heard.  Source-agnostic: a beacon, a CQ, a broadcast or
    a VMail all normalise to this.
    """
    callsign: str                      # as recorded, upper-cased
    heard_at: datetime                 # tz-aware UTC (the receiver's clock)
    source: str                        # beacon | cq | broadcast_gps | broadcast_grid | gps_tag
    source_ref: str                    # provenance, e.g. '9a1f|cqframe:79076'
    frame_kind: str                    # beacon | cq | broadcast | vmail

    grid: Optional[str] = None         # away-mark stripped, validated
    lat: Optional[float] = None
    lon: Optional[float] = None
    accuracy_m: Optional[float] = None

    snr_db: Optional[int] = None
    frequency_hz: Optional[int] = None
    band: Optional[str] = None
    bandwidth: Optional[str] = None

    is_own: bool = False               # our own transmission, as logged by VarAC
    is_away: bool = False
    is_emcomm: bool = False
    is_email_gateway: bool = False
    is_bbs: bool = False
    is_ai_gateway: bool = False
    has_diploma: bool = False
    cq_tag: Optional[str] = None       # 'POTA', 'DX', ...
    text: Optional[str] = None         # message text for broadcasts / vmails (display, not parsed)
    symbol: Optional[str] = None       # APRS symbol table+code, e.g. '/>' (aprs source only)
    is_object: bool = False            # APRS object/item rather than a station
    aprs_consent: Optional[bool] = None  # APRS:Y / APRS:N token in a VarAC broadcast; None = not stated

    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True)
class OwnFix:
    lat: float
    lon: float
    time: datetime
    source: str                        # gps_log | manual_ini | my_locator | nmea
    grid: Optional[str] = None
    speed_kmh: Optional[float] = None
    course_deg: Optional[float] = None
    altitude_m: Optional[float] = None
    accuracy_m: Optional[float] = None
    fix_quality: Optional[str] = None  # e.g. 'A' (RMC valid), 'V' (void)


@dataclass
class SourceHealth:
    source_id: str
    available: bool = False
    detail: str = ""
    last_ok_at: Optional[datetime] = None
    last_error: Optional[str] = None
    error_count: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)
