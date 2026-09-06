"""Emcomm BBS Welfare Board files, read-only.  The ONLY module that knows
Emcomm BBS (github.com/KK4ODA/emcomm-bbs) exists.

Emcomm BBS turns VarAC check-in files into a roster and writes, for the
current time window, ``welfare_YYYY-MM-DD_HHMM-HHMM.csv`` into VarAC's BBS
folder (``[BBS] BBSDirectory`` in VarAC.ini, ``C:\\VarAC BBS`` by default).
Columns: Date, Window, Callsign, Name, Location, Status, Power, Contact,
Message, Received_Time, Update_Number, Previous_Status.  Non-licensed
family members check in by name only (empty Callsign).

Facts this relies on:
* The file is rewritten in full after every accepted check-in, so a read can
  hit a half-written file: the caller keeps the last good parse on error.
* Times are the receiving PC's local clock, date + HH:MM:SS.
* Emcomm BBS may not be installed or running at all; then the folder or the
  file simply does not exist, which is a normal state, not an error.
"""
from __future__ import annotations

import csv
import glob
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..domain.callsign import normalise_callsign
from ..domain.timeparse import iso_utc

DEFAULT_BOARD_DIR = r"C:\VarAC BBS"
CSV_GLOB = "welfare_*.csv"

# Emcomm BBS status text -> short key used in CSS classes and filters.
STATUS_KEYS = {"SAFE": "safe", "NEED ASSISTANCE": "need", "TRAFFIC": "traffic"}


class WelfareBoardError(Exception):
    pass


def board_dir(cfg, vc) -> str:
    """Explicit setting > VarAC's BBS folder from VarAC.ini > the VarAC default."""
    explicit = (cfg.get("welfare", "board_dir") or "").strip()
    if explicit:
        return explicit
    try:
        from_ini = (vc.value("BBS", "BBSDirectory") or "").strip() if vc is not None else ""
    except Exception:  # noqa: BLE001 - never let VarAC.ini trouble break the welfare layer
        from_ini = ""
    return from_ini or DEFAULT_BOARD_DIR


def latest_board_csv(directory: str) -> Optional[str]:
    """Newest welfare CSV by modification time, or None when there is none."""
    if not directory or not os.path.isdir(directory):
        return None
    best, best_mtime = None, -1.0
    for path in glob.glob(os.path.join(directory, CSV_GLOB)):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def _received_iso(date_s: str, time_s: str) -> Optional[str]:
    """'2026-09-06' + '08:43:21' (local clock) -> UTC ISO string."""
    date_s, time_s = (date_s or "").strip(), (time_s or "").strip()
    if not date_s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            local = datetime.strptime(f"{date_s} {time_s}".strip(), fmt)
            break
        except ValueError:
            continue
    else:
        return None
    return iso_utc(local.astimezone(timezone.utc))


def parse_board_csv(path: str) -> Dict[str, Any]:
    """Parse one welfare CSV.  Raises WelfareBoardError when unreadable."""
    entries: Dict[str, Dict[str, Any]] = {}
    by_status: Dict[str, int] = {}
    date = window = ""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row:
                    continue
                get = lambda k: (row.get(k) or "").strip()  # noqa: E731
                status = get("Status").upper()
                name = get("Name")
                callsign, base = normalise_callsign(get("Callsign"))
                if not status or not (callsign or name):
                    continue        # header-only or half-written trailing row
                key = callsign or f"NAME:{name.upper()}"
                date = date or get("Date")
                window = window or get("Window")
                try:
                    update_number = int(get("Update_Number") or 0)
                except ValueError:
                    update_number = 0
                entry = {
                    "identifier": key,
                    "callsign": callsign or None,
                    "base_callsign": base or None,
                    "name": name,
                    "location": get("Location"),
                    "status": status,
                    "status_key": STATUS_KEYS.get(status, "other"),
                    "power": get("Power").upper() or None,
                    "contact": get("Contact") or None,
                    "message": get("Message"),
                    "received_at": _received_iso(get("Date"), get("Received_Time")),
                    "update_number": update_number,
                    "previous_status": get("Previous_Status") or None,
                }
                entries[key] = entry
                by_status[status] = by_status.get(status, 0) + 1
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        raise WelfareBoardError(f"cannot read {os.path.basename(path)}: {e}") from e
    return {"entries": entries, "by_status": by_status, "count": len(entries), "date": date, "window": window}


class WelfareBoard:
    """Parsed board with callsign lookup.  Immutable once built; the poller
    swaps whole instances so readers never see a half-updated board."""

    def __init__(self, parsed: Optional[Dict[str, Any]] = None) -> None:
        p = parsed or {"entries": {}, "by_status": {}, "count": 0, "date": "", "window": ""}
        self.entries: Dict[str, Dict[str, Any]] = p["entries"]
        self.by_status: Dict[str, int] = p["by_status"]
        self.count: int = p["count"]
        self.date: str = p["date"]
        self.window: str = p["window"]
        self._by_base: Dict[str, Dict[str, Any]] = {}
        for e in self.entries.values():
            if e.get("base_callsign") and e["base_callsign"] not in self._by_base:
                self._by_base[e["base_callsign"]] = e

    def lookup(self, callsign: str) -> Optional[Dict[str, Any]]:
        """Exact callsign first (KK4ODA/M is its own check-in), then the base
        callsign so a check-in from home also marks the mobile station."""
        cs, base = normalise_callsign(callsign)
        if not cs:
            return None
        hit = self.entries.get(cs)
        if hit is None and base:
            hit = self._by_base.get(base)
        return dict(hit) if hit else None

    def rows(self) -> List[Dict[str, Any]]:
        return sorted((dict(e) for e in self.entries.values()),
                      key=lambda e: (e.get("received_at") or ""), reverse=True)
