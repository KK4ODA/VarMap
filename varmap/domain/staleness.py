"""Staleness is a RENDERING attribute, never a deletion criterion.  Thresholds
key off the beacon interval (15 min default) rather than wall-clock intuition."""
from __future__ import annotations

from typing import Optional

STATES = ("fresh", "recent", "stale", "old", "historic")

DEFAULT_THRESHOLDS = {"fresh_minutes": 30, "recent_hours": 2, "stale_hours": 24, "hide_after_days": 30}


def classify(age_seconds: Optional[float], cfg: Optional[dict] = None) -> str:
    c = {**DEFAULT_THRESHOLDS, **(cfg or {})}
    if age_seconds is None:
        return "none"
    if age_seconds < c["fresh_minutes"] * 60:
        return "fresh"
    if age_seconds < c["recent_hours"] * 3600:
        return "recent"
    if age_seconds < c["stale_hours"] * 3600:
        return "stale"
    if age_seconds < c["hide_after_days"] * 86400:
        return "old"
    return "historic"
