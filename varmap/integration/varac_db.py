"""Read-only access to VarAC.db.  The ONLY module that knows VarAC's schema.

Non-negotiables (design doc 2.3.7, 6.10, 13.3):
* Open with mode=ro AND PRAGMA query_only=ON.  Never write.  Never VACUUM.
* Short transactions, busy_timeout, open/close per poll: VarAC runs with
  journal_mode=delete, so a lingering SHARED lock can block its writer.
* Explicit column lists, probed against PRAGMA table_info: VarAC's schema
  drifted v36 -> v41 in three months (additively).
* cqframe.id is AUTOINCREMENT and strictly monotonic in time (verified over
  79,076 rows), so an integer high-water mark never skips or repeats a frame.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from ..domain.callsign import normalise_callsign
from ..domain.gpstag import parse_aprs_consent, parse_position_text
from ..domain.grid import grid_accuracy_m, grid_to_latlon, normalise_grid, split_locator
from ..domain.timeparse import parse_varac_time
from .contracts import Observation

CQFRAME_TYPE_CQ = 1
CQFRAME_TYPE_BEACON = 2
SQLITE_BUSY_TIMEOUT_MS = 5000

REQUIRED_CQ = ["id", "guid", "cqframe_time", "cqframe_type_id", "from_callsign", "locator",
               "frequency", "band", "snr"]
OPTIONAL_CQ = ["bandwidth", "slot", "data", "is_emcomm", "is_email_gateway", "is_ai_gateway",
               "is_bbs", "diploma", "instance_id", "is_scanning"]
BROADCAST_COLS = ["id", "guid", "broadcast_time", "frequency", "from_callsign", "to_callsign",
                  "via_callsign", "broadcast_message", "snr", "band"]
VMAIL_COLS = ["id", "guid", "creation_time", "sent_time", "received_time", "folder_id",
              "vmail_to", "vmail_from", "vmail_via", "delivery_band", "delivery_snr", "subject", "msg"]


class IncompatibleSchema(Exception):
    pass


class NotAVaracDatabase(Exception):
    pass


def open_varac_ro(path: str) -> sqlite3.Connection:
    """Open VarAC.db strictly read-only.  Never mutates VarAC's data."""
    uri = f"file:{quote(path.replace(chr(92), '/'))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _tables(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _param(conn: sqlite3.Connection, name: str) -> Optional[str]:
    try:
        r = conn.execute("SELECT parameter_value FROM parameter WHERE parameter_name=?", (name,)).fetchone()
        return r[0] if r else None
    except sqlite3.Error:
        return None


def identity_token(first_login_time: Optional[str], path: str) -> str:
    """Short stable token qualifying source_ref, so a restored/replaced
    database never collides with rows ingested from the previous one."""
    basis = (first_login_time or "") + "|" + (path or "")
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:8]


def validate_database(path: str) -> Dict[str, Any]:
    """Cheap check that a file is a VarAC database we can use.  Never raises."""
    info: Dict[str, Any] = {"ok": False, "path": path}
    try:
        conn = open_varac_ro(path)
    except sqlite3.Error as e:
        info["error"] = f"cannot open: {e}"
        return info
    try:
        tables = _tables(conn)
        missing = {"cqframe", "cqframe_type"} - tables
        if missing:
            info["error"] = f"not a VarAC database (missing {sorted(missing)})"
            return info
        cols = set(_table_columns(conn, "cqframe"))
        req_missing = set(REQUIRED_CQ) - cols
        if req_missing:
            info["error"] = f"cqframe is missing required columns {sorted(req_missing)}"
            return info
        info.update({
            "ok": True,
            "db_version": _param(conn, "db_version"),
            "first_login_time": _param(conn, "first_login_time"),
            "last_login_time": _param(conn, "last_login_time"),
            "cqframe_rows": conn.execute("SELECT COUNT(*) FROM cqframe").fetchone()[0],
            "cqframe_max_id": conn.execute("SELECT MAX(id) FROM cqframe").fetchone()[0] or 0,
            "has_broadcast": "broadcast" in tables,
            "has_vmail": "vmail" in tables,
            "has_contact": "contact" in tables,
            "optional_columns": sorted(set(OPTIONAL_CQ) & cols),
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        })
        return info
    except sqlite3.Error as e:
        info["error"] = str(e)
        return info
    finally:
        conn.close()


class VaracDbSource:
    """Stateless helpers over one read-only connection.  The caller owns the
    connection lifetime (open, poll, close) and the cursors."""

    def __init__(self, path: str, mycall: str = "") -> None:
        self.path = path
        self.mycall = (mycall or "").upper()
        self.mybase = normalise_callsign(self.mycall)[1] if self.mycall else ""
        self.cq_columns: List[str] = []
        self.has_broadcast = False
        self.has_vmail = False
        self.has_contact = False
        self.db_version: Optional[str] = None
        self.first_login_time: Optional[str] = None
        self.identity: str = ""

    # -- lifecycle -------------------------------------------------------
    def open(self) -> sqlite3.Connection:
        return open_varac_ro(self.path)

    def probe(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        tables = _tables(conn)
        if "cqframe" not in tables:
            raise NotAVaracDatabase("cqframe table not found")
        cols = set(_table_columns(conn, "cqframe"))
        missing = set(REQUIRED_CQ) - cols
        if missing:
            raise IncompatibleSchema(f"cqframe is missing required columns: {sorted(missing)}")
        self.cq_columns = REQUIRED_CQ + [c for c in OPTIONAL_CQ if c in cols]
        self.has_broadcast = "broadcast" in tables and set(BROADCAST_COLS) <= set(_table_columns(conn, "broadcast"))
        self.has_vmail = "vmail" in tables and set(VMAIL_COLS) <= set(_table_columns(conn, "vmail"))
        self.has_contact = "contact" in tables
        self.db_version = _param(conn, "db_version")
        self.first_login_time = _param(conn, "first_login_time")
        self.identity = identity_token(self.first_login_time, self.path)
        return {
            "db_version": self.db_version,
            "first_login_time": self.first_login_time,
            "identity": self.identity,
            "cq_columns": list(self.cq_columns),
            "has_broadcast": self.has_broadcast,
            "has_vmail": self.has_vmail,
            "has_contact": self.has_contact,
        }

    # -- max ids (cursor sanity) ------------------------------------------
    def max_ids(self, conn: sqlite3.Connection) -> Dict[str, int]:
        out = {"cqframe": conn.execute("SELECT COALESCE(MAX(id),0) FROM cqframe").fetchone()[0]}
        if self.has_broadcast:
            out["broadcast"] = conn.execute("SELECT COALESCE(MAX(id),0) FROM broadcast").fetchone()[0]
        if self.has_vmail:
            out["vmail"] = conn.execute("SELECT COALESCE(MAX(id),0) FROM vmail").fetchone()[0]
        return out

    def min_id_since(self, conn: sqlite3.Connection, table: str, time_col: str, cutoff: str) -> int:
        """Smallest id at/after a VarAC-format cutoff ('YYYY-MM-DD HH:MM:SS'), for bounded backfill."""
        r = conn.execute(f"SELECT COALESCE(MIN(id),0) FROM {table} WHERE {time_col} >= ?", (cutoff,)).fetchone()
        return int(r[0] or 0)

    # -- fetches ----------------------------------------------------------
    def fetch_cqframes(self, conn: sqlite3.Connection, hwm: int, batch: int) -> List[sqlite3.Row]:
        cols = ", ".join(self.cq_columns)
        return conn.execute(
            f"SELECT {cols} FROM cqframe WHERE id > ? ORDER BY id ASC LIMIT ?", (hwm, batch)).fetchall()

    def fetch_broadcasts(self, conn: sqlite3.Connection, hwm: int, batch: int) -> List[sqlite3.Row]:
        if not self.has_broadcast:
            return []
        cols = ", ".join(BROADCAST_COLS)
        return conn.execute(
            f"SELECT {cols} FROM broadcast WHERE id > ? ORDER BY id ASC LIMIT ?", (hwm, batch)).fetchall()

    def fetch_vmails(self, conn: sqlite3.Connection, hwm: int, batch: int) -> List[sqlite3.Row]:
        if not self.has_vmail:
            return []
        cols = ", ".join(VMAIL_COLS)
        return conn.execute(
            f"SELECT {cols} FROM vmail WHERE id > ? ORDER BY id ASC LIMIT ?", (hwm, batch)).fetchall()

    def fetch_contacts(self, conn: sqlite3.Connection) -> Dict[str, Tuple[str, str, str]]:
        """callsign -> (name, qth, locator) from VarAC's contact cache.  Enrichment only;
        never a position source (unclear update semantics)."""
        if not self.has_contact:
            return {}
        out: Dict[str, Tuple[str, str, str]] = {}
        for r in conn.execute("SELECT callsign, name, qth, locator FROM contact WHERE is_deleted=0"):
            cs = (r["callsign"] or "").strip().upper()
            if cs:
                out[cs] = ((r["name"] or "").strip(), (r["qth"] or "").strip(), (r["locator"] or "").strip())
        return out

    # -- row -> Observation -------------------------------------------------
    def _is_own(self, callsign: str, snr: Any) -> bool:
        if self.mycall and (callsign == self.mycall or normalise_callsign(callsign)[1] == self.mybase):
            return True
        return snr is None and bool(self.mycall) and callsign == self.mycall

    def cqframe_to_observation(self, row: sqlite3.Row) -> Optional[Observation]:
        d = dict(row)
        callsign, _ = normalise_callsign(d.get("from_callsign") or "")
        heard_at = parse_varac_time(d.get("cqframe_time"))
        if not callsign or heard_at is None:
            return None
        kind = "beacon" if d.get("cqframe_type_id") == CQFRAME_TYPE_BEACON else "cq"
        grid_raw, away = split_locator(d.get("locator"))
        grid = normalise_grid(grid_raw)
        lat = lon = acc = None
        if grid:
            c = grid_to_latlon(grid)
            if c:
                lat, lon = c
                acc = grid_accuracy_m(grid)
        tag = (d.get("data") or "").strip() or None
        return Observation(
            callsign=callsign, heard_at=heard_at, source=kind,
            source_ref=f"{self.identity}|cqframe:{d['id']}", frame_kind=kind,
            grid=grid, lat=lat, lon=lon, accuracy_m=acc,
            snr_db=d.get("snr"), frequency_hz=d.get("frequency"), band=d.get("band"),
            bandwidth=(d.get("bandwidth") or None),
            is_own=self._is_own(callsign, d.get("snr")), is_away=away,
            is_emcomm=bool(d.get("is_emcomm")), is_email_gateway=bool(d.get("is_email_gateway")),
            is_bbs=bool(d.get("is_bbs")), is_ai_gateway=bool(d.get("is_ai_gateway")),
            has_diploma=(str(d.get("diploma") or "").strip() == "1"), cq_tag=tag,
            raw={k: v for k, v in d.items() if k != "guid"},
        )

    def broadcast_to_observation(self, row: sqlite3.Row) -> Optional[Observation]:
        d = dict(row)
        callsign, _ = normalise_callsign(d.get("from_callsign") or "")
        heard_at = parse_varac_time(d.get("broadcast_time"))
        if not callsign or heard_at is None:
            return None
        text = d.get("broadcast_message") or ""
        pos = parse_position_text(text)
        source = "broadcast"
        grid = lat = lon = acc = None
        if pos:
            source = "broadcast_gps" if pos.kind == "gps" else "broadcast_grid"
            lat, lon, grid, acc = pos.lat, pos.lon, pos.grid, pos.accuracy_m
        return Observation(
            callsign=callsign, heard_at=heard_at, source=source,
            source_ref=f"{self.identity}|broadcast:{d['id']}", frame_kind="broadcast",
            grid=grid, lat=lat, lon=lon, accuracy_m=acc,
            snr_db=d.get("snr"), frequency_hz=d.get("frequency"), band=d.get("band"),
            is_own=self._is_own(callsign, d.get("snr")),
            text=text[:200], aprs_consent=parse_aprs_consent(text),
            raw={"id": d["id"], "to": d.get("to_callsign"), "via": d.get("via_callsign")},
        )

    def vmail_to_observation(self, row: sqlite3.Row) -> Optional[Observation]:
        """Only VMails carrying a <GPS:...> tag become observations."""
        d = dict(row)
        callsign, _ = normalise_callsign(d.get("vmail_from") or "")
        heard_at = (parse_varac_time(d.get("received_time")) or parse_varac_time(d.get("sent_time"))
                    or parse_varac_time(d.get("creation_time")))
        if not callsign or heard_at is None:
            return None
        pos = parse_position_text(d.get("msg") or "")
        if not pos or pos.kind != "gps":
            return None
        snr = None
        try:
            snr = int(str(d.get("delivery_snr") or "").strip() or "x")
        except ValueError:
            snr = None
        return Observation(
            callsign=callsign, heard_at=heard_at, source="gps_tag",
            source_ref=f"{self.identity}|vmail:{d['id']}", frame_kind="vmail",
            lat=pos.lat, lon=pos.lon, accuracy_m=pos.accuracy_m,
            snr_db=snr, band=d.get("delivery_band"),
            is_own=(callsign == self.mycall), text=(d.get("subject") or "")[:120],
            raw={"id": d["id"], "folder_id": d.get("folder_id"), "gps_raw": pos.raw},
        )


def rows_to_json(rows: Iterable[sqlite3.Row]) -> str:
    return json.dumps([dict(r) for r in rows], default=str)
