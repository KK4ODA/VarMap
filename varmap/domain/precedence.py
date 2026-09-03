"""Which observation becomes a station's authoritative current position.

Ranks (higher is better):
    manual          4   operator override
    gps_tag         3   exact coordinates deliberately shared in a VMail
    broadcast_gps   3   exact coordinates in a broadcast
    beacon / cq     2   grid transmitted in that specific frame
    broadcast_grid  1   grid pulled out of free text (LOC tag or bare grid)

Rule: a newer observation of equal-or-higher rank always wins.  Across ranks
in either direction, the "wrong-direction" candidate only wins while the other
position is still within `cross_rank_max_age`: a precise <GPS:> from three
days ago must not override a grid heard ten minutes ago, and a bare grid
scraped from a broadcast must not override a fresh precise fix.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

SOURCE_RANK = {
    "manual": 4,
    "gps_tag": 3,
    "broadcast_gps": 3,
    "aprs": 3,           # exact coordinates from an APRS packet (via Graywolf)
    "beacon": 2,
    "cq": 2,
    "relayed": 2,        # exact coordinates but second-hand (relayed by another station)
    "broadcast_grid": 1,
}
DEFAULT_CROSS_RANK_MAX_AGE = timedelta(hours=6)


def rank_of(source: Optional[str]) -> int:
    return SOURCE_RANK.get(source or "", 0)


def should_replace(current_source: Optional[str], current_time: Optional[datetime],
                   incoming_source: str, incoming_time: datetime,
                   now: datetime, cross_rank_max_age: timedelta = DEFAULT_CROSS_RANK_MAX_AGE) -> bool:
    if current_time is None or current_source is None:
        return True
    ri, rc = rank_of(incoming_source), rank_of(current_source)
    if incoming_time >= current_time:
        if ri >= rc:
            return True
        # Newer but lower rank: only if the better position has gone stale.
        return (now - current_time) > cross_rank_max_age
    # Older than current: only a strictly better source, still fresh, may win.
    if ri > rc:
        return (now - incoming_time) <= cross_rank_max_age
    return False
