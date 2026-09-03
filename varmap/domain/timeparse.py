"""Timestamps.  VarAC writes .NET round-trip style UTC with SEVEN fractional
digits and a literal Z ('2026-09-02 01:12:33.5707779Z'), except some tables
which use second precision without the Z.  Everything is UTC."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(?:Z|\+00:00)?$")


def parse_varac_time(s: Optional[str]) -> Optional[datetime]:
    """VarAC timestamp -> tz-aware UTC datetime.  None on failure."""
    if not s:
        return None
    m = _TS.match(str(s).strip())
    if not m:
        return None
    date, tod, frac = m.groups()
    micros = int((frac or "0").ljust(6, "0")[:6])
    try:
        return datetime.strptime(f"{date} {tod}", "%Y-%m-%d %H:%M:%S").replace(
            microsecond=micros, tzinfo=timezone.utc)
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Canonical storage form: 'YYYY-MM-DDTHH:MM:SS.ffffffZ' (sorts lexically)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse our own canonical form (also tolerates VarAC's)."""
    return parse_varac_time(s)


def age_seconds(dt: Optional[datetime], now: Optional[datetime] = None) -> Optional[float]:
    if dt is None:
        return None
    return ((now or now_utc()) - dt).total_seconds()


def friendly_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60} min"
    d = s // 86400
    return f"{d} d {(s % 86400) // 3600} h"


def cutoff_days_ago(days: float, now: Optional[datetime] = None) -> datetime:
    return (now or now_utc()) - timedelta(days=days)
