"""Callsign normalisation.  KK4ODA/P and KK4ODA are DIFFERENT stations (a
portable site vs a home QTH); we never merge them, but we expose a base
callsign so the UI can group them on request."""
from __future__ import annotations

from typing import Tuple


def normalise_callsign(raw: str) -> Tuple[str, str]:
    """-> (canonical, base).  'f/kk4oda/p' -> ('F/KK4ODA/P', 'KK4ODA')."""
    cs = (raw or "").strip().upper()
    if not cs:
        return "", ""
    parts = [p for p in cs.split("/") if p]
    if not parts:
        return cs, cs
    cands = [p for p in parts if any(ch.isdigit() for ch in p)]
    base = max(cands, key=len) if cands else parts[0]
    return cs, base
