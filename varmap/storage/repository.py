"""SQLite persistence for VarMap.  One writer (the poller) plus readers; WAL.

The load-bearing property: the source cursor is advanced INSIDE the same
transaction as the data writes, and every observation has a UNIQUE
source_ref, so a crash mid-batch is replayed harmlessly.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from ..domain.callsign import normalise_callsign
from ..domain.geo import implied_speed_kmh
from ..domain.precedence import should_replace
from ..domain.timeparse import iso_utc, now_utc, parse_iso
from ..integration.contracts import Observation, OwnFix

log = logging.getLogger("varmap.repo")
SCHEMA_VERSION = 1
SUSPECT_SPEED_KMH = 900.0


@dataclass
class IngestStats:
    observations: int = 0
    heard_inserted: int = 0
    positions_inserted: int = 0
    positions_replaced: int = 0
    suspect: int = 0
    skipped: int = 0
    stations_touched: int = 0
    errors: List[str] = field(default_factory=list)


class Repository:
    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.quarantined: Optional[str] = None
        self.integrity: str = "unchecked"
        self._check_and_quarantine()
        self.init_schema()

    def _check_and_quarantine(self) -> None:
        """Start-up integrity check.  Our database is a cache of VarAC's history, so a
        damaged file is moved aside and rebuilt rather than nursed along.  User notes
        and favourites in the damaged file are lost; everything else comes back."""
        if not os.path.isfile(self.path):
            self.integrity = "new"
            return
        try:
            c = sqlite3.connect(self.path, timeout=15)
            try:
                row = c.execute("PRAGMA quick_check").fetchone()
                self.integrity = "ok" if row and row[0] == "ok" else f"damaged: {row[0] if row else '?'}"
            finally:
                c.close()
        except sqlite3.DatabaseError as e:
            self.integrity = f"damaged: {e}"
        if self.integrity == "ok":
            return
        stamp = now_utc().strftime("%Y%m%d-%H%M%S")
        moved = []
        for suffix in ("", "-wal", "-shm", "-journal"):
            src = self.path + suffix
            if os.path.isfile(src):
                dst = f"{self.path}.damaged-{stamp}{suffix}"
                try:
                    os.replace(src, dst)
                    moved.append(dst)
                except OSError as e:
                    log.error("could not quarantine %s: %s", src, e)
        self.quarantined = moved[0] if moved else None
        log.error("station database failed its integrity check (%s); moved aside as %s and rebuilding from VarAC",
                  self.integrity, self.quarantined)

    # -- connections -----------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA synchronous = NORMAL")
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 15000")
            self._local.conn = c
        return c

    def init_schema(self) -> None:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "schema.sql"), "r", encoding="utf-8") as f:
            sql = f.read()
        c = self.conn()
        with self._write_lock:
            c.executescript(sql)
            c.execute("INSERT OR IGNORE INTO app_meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            # Additive migrations for databases created by earlier versions.
            cols = {r[1] for r in c.execute("PRAGMA table_info(beacon_tx)")}
            if "frequency_hz" not in cols:
                c.execute("ALTER TABLE beacon_tx ADD COLUMN frequency_hz INTEGER")
            scols = {r[1] for r in c.execute("PRAGMA table_info(station)")}
            if "aprs_symbol" not in scols:
                c.execute("ALTER TABLE station ADD COLUMN aprs_symbol TEXT")
            if "is_object" not in scols:
                c.execute("ALTER TABLE station ADD COLUMN is_object INTEGER NOT NULL DEFAULT 0")
            if "aprs_consent" not in scols:
                c.execute("ALTER TABLE station ADD COLUMN aprs_consent INTEGER")
                c.execute("ALTER TABLE station ADD COLUMN aprs_consent_at TEXT")
        self._repair_relay_attribution()

    def _repair_relay_attribution(self) -> int:
        """One-time repair (versions <= 0.3.0): positions parsed out of relay-format
        broadcasts ('APRS X <GPS:..> via Y') were attributed to the SENDER.  Remove
        those position rows and recompute the affected stations' current position."""
        if self.meta_get("repair_relay_attribution") == "1":
            return 0
        c = self.conn()
        with self._write_lock:
            bad = [dict(r) for r in c.execute(
                "SELECT sp.id, sp.callsign, sp.source_ref FROM station_position sp JOIN station_heard sh "
                "ON sh.source_ref = sp.source_ref WHERE sh.frame_kind='broadcast' AND "
                "(sh.text LIKE 'APRS % via %' OR sh.text LIKE 'RELAY % via %' OR sh.text LIKE 'POS % via %')")]
            for b in bad:
                c.execute("DELETE FROM station_position WHERE id=?", (b["id"],))
                c.execute("UPDATE station SET position_count=MAX(position_count-1, 0) WHERE callsign=?", (b["callsign"],))
            for cs in {b["callsign"] for b in bad}:
                best = c.execute("SELECT * FROM station_position WHERE callsign=? ORDER BY heard_at DESC LIMIT 1", (cs,)).fetchone()
                if best:
                    c.execute("UPDATE station SET lat=?, lon=?, grid=?, accuracy_m=?, position_source=?, position_time=?, "
                              "position_ref=?, position_suspect=?, updated_at=? WHERE callsign=?",
                              (best["lat"], best["lon"], best["grid"], best["accuracy_m"], best["source"], best["heard_at"],
                               best["source_ref"], best["suspect"], iso_utc(now_utc()), cs))
                else:
                    c.execute("UPDATE station SET lat=NULL, lon=NULL, grid=NULL, accuracy_m=NULL, position_source=NULL, "
                              "position_time=NULL, position_ref=NULL, updated_at=? WHERE callsign=?", (iso_utc(now_utc()), cs))
            c.execute("INSERT INTO app_meta(key, value) VALUES('repair_relay_attribution','1') "
                      "ON CONFLICT(key) DO UPDATE SET value='1'")
        if bad:
            log.info("repaired %d position(s) wrongly attributed to relaying stations", len(bad))
        return len(bad)

    # -- meta / cursors ---------------------------------------------------
    def meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        r = self.conn().execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def meta_set(self, key: str, value: str) -> None:
        with self._write_lock:
            self.conn().execute("INSERT INTO app_meta(key, value) VALUES(?, ?) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def cursor_get(self, source_id: str) -> Optional[Dict[str, Any]]:
        r = self.conn().execute("SELECT * FROM source_cursor WHERE source_id=?", (source_id,)).fetchone()
        return dict(r) if r else None

    def cursor_set(self, source_id: str, cursor: str, ok: bool = True, error: Optional[str] = None) -> None:
        with self._write_lock:
            self._cursor_set(self.conn(), source_id, cursor, ok, error)

    @staticmethod
    def _cursor_set(c: sqlite3.Connection, source_id: str, cursor: str, ok: bool, error: Optional[str]) -> None:
        now = iso_utc(now_utc())
        c.execute(
            "INSERT INTO source_cursor(source_id, cursor, updated_at, last_ok_at, last_error, error_count) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET "
            "cursor=excluded.cursor, updated_at=excluded.updated_at, "
            "last_ok_at=CASE WHEN ? THEN excluded.updated_at ELSE source_cursor.last_ok_at END, "
            "last_error=?, error_count=CASE WHEN ? THEN 0 ELSE source_cursor.error_count + 1 END",
            (source_id, cursor, now, now if ok else None, error, 0 if ok else 1, ok, error, ok))

    def cursor_error(self, source_id: str, error: str) -> None:
        with self._write_lock:
            c = self.conn()
            r = c.execute("SELECT cursor FROM source_cursor WHERE source_id=?", (source_id,)).fetchone()
            self._cursor_set(c, source_id, r[0] if r else "0", False, error)

    # -- ingest -----------------------------------------------------------
    def ingest(self, observations: Iterable[Observation], cursors: Dict[str, str],
               now: Optional[datetime] = None) -> IngestStats:
        """Apply a batch of observations and advance cursors in ONE transaction."""
        now = now or now_utc()
        now_iso = iso_utc(now)
        stats = IngestStats()
        c = self.conn()
        cache: Dict[str, Dict[str, Any]] = {}
        with self._write_lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                for obs in observations:
                    stats.observations += 1
                    try:
                        self._apply(c, obs, now, now_iso, cache, stats)
                    except Exception as e:  # never abort a batch for one bad row
                        stats.skipped += 1
                        if len(stats.errors) < 20:
                            stats.errors.append(f"{obs.source_ref}: {e}")
                        log.warning("ingest: skipped %s: %s", obs.source_ref, e)
                for sid, cur in cursors.items():
                    self._cursor_set(c, sid, str(cur), True, None)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        stats.stations_touched = len(cache)
        return stats

    def _station_row(self, c: sqlite3.Connection, callsign: str, cache: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if callsign in cache:
            return cache[callsign]
        r = c.execute("SELECT callsign, lat, lon, position_source, position_time, first_heard, last_heard "
                      "FROM station WHERE callsign=?", (callsign,)).fetchone()
        row = dict(r) if r else None
        if row is not None:
            cache[callsign] = row
        return row

    def _apply(self, c: sqlite3.Connection, obs: Observation, now: datetime, now_iso: str,
               cache: Dict[str, Dict[str, Any]], stats: IngestStats) -> None:
        callsign, base = normalise_callsign(obs.callsign)
        if not callsign:
            stats.skipped += 1
            return
        heard_iso = iso_utc(obs.heard_at)

        cur = c.execute(
            "INSERT OR IGNORE INTO station_heard(callsign, heard_at, source_ref, frame_kind, had_position, "
            "snr_db, band, frequency_hz, text) VALUES(?,?,?,?,?,?,?,?,?)",
            (callsign, heard_iso, obs.source_ref, obs.frame_kind, 1 if obs.has_position else 0,
             obs.snr_db, obs.band, obs.frequency_hz, obs.text))
        if cur.rowcount == 0:
            return  # already ingested: replay is a no-op
        stats.heard_inserted += 1

        st = self._station_row(c, callsign, cache)
        if st is None:
            c.execute(
                "INSERT INTO station(callsign, base_callsign, is_own, first_heard, last_heard, heard_count, updated_at) "
                "VALUES(?,?,?,?,?,0,?)", (callsign, base, 1 if obs.is_own else 0, heard_iso, heard_iso, now_iso))
            st = {"callsign": callsign, "lat": None, "lon": None, "position_source": None,
                  "position_time": None, "first_heard": heard_iso, "last_heard": heard_iso}
            cache[callsign] = st

        # Heard-state update.  Only latch radio metadata/flags from the newest frame.
        newest = heard_iso >= (st["last_heard"] or "")
        if newest:
            c.execute(
                "UPDATE station SET last_heard=?, heard_count=heard_count+1, last_snr_db=?, last_frequency_hz=?, "
                "last_band=?, last_bandwidth=?, last_frame_kind=?, last_text=COALESCE(?, last_text), "
                "is_away=?, is_emcomm=?, is_email_gateway=CASE WHEN ? THEN ? ELSE is_email_gateway END, "
                "is_bbs=CASE WHEN ? THEN ? ELSE is_bbs END, is_ai_gateway=CASE WHEN ? THEN ? ELSE is_ai_gateway END, "
                "has_diploma=CASE WHEN ? THEN ? ELSE has_diploma END, last_cq_tag=COALESCE(?, last_cq_tag), "
                "is_own=CASE WHEN ? THEN 1 ELSE is_own END, aprs_symbol=COALESCE(?, aprs_symbol), "
                "is_object=CASE WHEN ? THEN 1 ELSE is_object END, updated_at=? WHERE callsign=?",
                (heard_iso, obs.snr_db, obs.frequency_hz, obs.band, obs.bandwidth, obs.frame_kind, obs.text,
                 1 if obs.is_away else 0, 1 if obs.is_emcomm else 0,
                 obs.frame_kind == "beacon", 1 if obs.is_email_gateway else 0,
                 obs.frame_kind == "beacon", 1 if obs.is_bbs else 0,
                 obs.frame_kind == "beacon", 1 if obs.is_ai_gateway else 0,
                 obs.frame_kind == "beacon", 1 if obs.has_diploma else 0,
                 obs.cq_tag, obs.is_own, obs.symbol, obs.is_object, now_iso, callsign))
            st["last_heard"] = heard_iso
        else:
            c.execute("UPDATE station SET heard_count=heard_count+1, first_heard=MIN(first_heard, ?), updated_at=? "
                      "WHERE callsign=?", (heard_iso, now_iso, callsign))
        if obs.aprs_consent is not None:
            # The newest statement wins; an APRS:N after an APRS:Y withdraws consent.
            c.execute("UPDATE station SET aprs_consent=?, aprs_consent_at=? WHERE callsign=? "
                      "AND (aprs_consent_at IS NULL OR aprs_consent_at <= ?)",
                      (1 if obs.aprs_consent else 0, heard_iso, callsign, heard_iso))
        if heard_iso < (st["first_heard"] or heard_iso):
            c.execute("UPDATE station SET first_heard=? WHERE callsign=?", (heard_iso, callsign))
            st["first_heard"] = heard_iso

        if not obs.has_position:
            return

        # Implausible-jump check against the station's current authoritative position.
        suspect = 0
        if st.get("lat") is not None and st.get("position_time"):
            pt = parse_iso(st["position_time"])
            if pt is not None:
                a, b = (pt, obs.heard_at) if pt <= obs.heard_at else (obs.heard_at, pt)
                sp = implied_speed_kmh(st["lat"], st["lon"], a, obs.lat, obs.lon, b) if a != b else None
                if sp is not None and sp > SUSPECT_SPEED_KMH:
                    suspect = 1
                    stats.suspect += 1

        cur = c.execute(
            "INSERT OR IGNORE INTO station_position(callsign, heard_at, received_at, lat, lon, grid, accuracy_m, "
            "source, source_ref, snr_db, frequency_hz, band, is_away, suspect, raw_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (callsign, heard_iso, now_iso, obs.lat, obs.lon, obs.grid, obs.accuracy_m, obs.source, obs.source_ref,
             obs.snr_db, obs.frequency_hz, obs.band, 1 if obs.is_away else 0, suspect,
             json.dumps(obs.raw, default=str) if obs.raw else None))
        if cur.rowcount == 0:
            return
        stats.positions_inserted += 1
        c.execute("UPDATE station SET position_count=position_count+1 WHERE callsign=?", (callsign,))

        if should_replace(st.get("position_source"), parse_iso(st.get("position_time")), obs.source, obs.heard_at, now):
            c.execute(
                "UPDATE station SET lat=?, lon=?, grid=?, accuracy_m=?, position_source=?, position_time=?, "
                "position_received=?, position_ref=?, position_suspect=?, updated_at=? WHERE callsign=?",
                (obs.lat, obs.lon, obs.grid, obs.accuracy_m, obs.source, heard_iso, now_iso, obs.source_ref,
                 suspect, now_iso, callsign))
            st.update({"lat": obs.lat, "lon": obs.lon, "position_source": obs.source, "position_time": heard_iso})
            stats.positions_replaced += 1

    # -- enrichment ---------------------------------------------------------
    def enrich_from_contacts(self, contacts: Dict[str, Any]) -> int:
        n = 0
        with self._write_lock:
            c = self.conn()
            c.execute("BEGIN")
            try:
                for cs, (name, qth, _grid) in contacts.items():
                    if not (name or qth):
                        continue
                    cur = c.execute("UPDATE station SET op_name=COALESCE(NULLIF(op_name,''), ?), "
                                    "qth=COALESCE(NULLIF(qth,''), ?) WHERE callsign=? AND "
                                    "(op_name IS NULL OR op_name='' OR qth IS NULL OR qth='')", (name or None, qth or None, cs))
                    n += cur.rowcount
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return n

    def set_station_user_fields(self, callsign: str, **fields: Any) -> bool:
        allowed = {"notes", "is_favorite", "is_hidden", "op_name", "qth"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        cols = ", ".join(f"{k}=?" for k in sets)
        with self._write_lock:
            cur = self.conn().execute(f"UPDATE station SET {cols}, updated_at=? WHERE callsign=?",
                                      (*sets.values(), iso_utc(now_utc()), callsign.upper()))
        return cur.rowcount > 0

    # -- queries --------------------------------------------------------------
    def stations(self, since_iso: Optional[str] = None, include_hidden: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM station"
        where, args = [], []
        if since_iso:
            where.append("updated_at > ?")
            args.append(since_iso)
        if not include_hidden:
            where.append("is_hidden = 0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_heard DESC"
        return [dict(r) for r in self.conn().execute(sql, args)]

    def station(self, callsign: str) -> Optional[Dict[str, Any]]:
        r = self.conn().execute("SELECT * FROM station WHERE callsign=?", (callsign.upper(),)).fetchone()
        return dict(r) if r else None

    def positions(self, callsign: str, limit: int = 1000, since_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT id, heard_at, lat, lon, grid, accuracy_m, source, source_ref, snr_db, band, is_away, suspect " \
              "FROM station_position WHERE callsign=?"
        args: List[Any] = [callsign.upper()]
        if since_iso:
            sql += " AND heard_at >= ?"
            args.append(since_iso)
        sql += " ORDER BY heard_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn().execute(sql, args)]

    def heard(self, callsign: str, limit: int = 50) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn().execute(
            "SELECT heard_at, frame_kind, had_position, snr_db, band, frequency_hz, text FROM station_heard "
            "WHERE callsign=? ORDER BY heard_at DESC LIMIT ?", (callsign.upper(), limit))]

    def recent_bands(self, hours: float = 6.0) -> Dict[str, List[str]]:
        """callsign -> bands heard on within the last `hours` (any frame kind)."""
        since = iso_utc(now_utc() - timedelta(hours=hours))
        out: Dict[str, List[str]] = {}
        for r in self.conn().execute(
                "SELECT callsign, band FROM station_heard WHERE heard_at >= ? AND band IS NOT NULL AND band <> '' "
                "GROUP BY callsign, band ORDER BY callsign, MAX(heard_at) DESC", (since,)):
            out.setdefault(r[0], []).append(r[1])
        return out

    def recent_broadcasts(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn().execute(
            "SELECT callsign, heard_at, snr_db, band, text FROM station_heard WHERE frame_kind='broadcast' "
            "ORDER BY heard_at DESC LIMIT ?", (limit,))]

    def counts(self) -> Dict[str, Any]:
        c = self.conn()
        day_ago = iso_utc(now_utc() - timedelta(hours=24))
        return {
            "stations": c.execute("SELECT COUNT(*) FROM station").fetchone()[0],
            "stations_with_position": c.execute("SELECT COUNT(*) FROM station WHERE lat IS NOT NULL").fetchone()[0],
            "heard_24h": c.execute("SELECT COUNT(DISTINCT callsign) FROM station_heard WHERE heard_at >= ?", (day_ago,)).fetchone()[0],
            "frames": c.execute("SELECT COUNT(*) FROM station_heard").fetchone()[0],
            "positions": c.execute("SELECT COUNT(*) FROM station_position").fetchone()[0],
            "newest_heard": c.execute("SELECT MAX(last_heard) FROM station").fetchone()[0],
            "db_bytes": os.path.getsize(self.path) if os.path.isfile(self.path) else 0,
            "db_integrity": self.integrity,
            "db_quarantined": self.quarantined,
        }

    # -- own position -----------------------------------------------------------
    def own_position_add(self, fix: OwnFix) -> None:
        with self._write_lock:
            self.conn().execute(
                "INSERT INTO own_position(recorded_at, fix_time, lat, lon, grid, source, speed_kmh, course_deg, "
                "altitude_m, accuracy_m) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (iso_utc(now_utc()), iso_utc(fix.time), fix.lat, fix.lon, fix.grid, fix.source, fix.speed_kmh,
                 fix.course_deg, fix.altitude_m, fix.accuracy_m))

    def own_position_latest(self) -> Optional[Dict[str, Any]]:
        r = self.conn().execute("SELECT * FROM own_position ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def own_position_history(self, since_iso: str, limit: int = 5000) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn().execute(
            "SELECT recorded_at, fix_time, lat, lon, grid, source, speed_kmh, course_deg FROM own_position "
            "WHERE recorded_at >= ? ORDER BY id ASC LIMIT ?", (since_iso, limit))]

    # -- beacon log ---------------------------------------------------------------
    def beacon_tx_add(self, **row: Any) -> int:
        cols = ["requested_at", "sent_at", "lat", "lon", "grid", "trigger", "method", "message", "dry_run", "ok", "error",
                "frequency_hz"]
        vals = [row.get(k) for k in cols]
        with self._write_lock:
            cur = self.conn().execute(
                f"INSERT INTO beacon_tx({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", vals)
            return cur.lastrowid

    def beacon_tx_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn().execute("SELECT * FROM beacon_tx ORDER BY id DESC LIMIT ?", (limit,))]

    def beacon_tx_count_since(self, since_iso: str, real_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM beacon_tx WHERE ok=1 AND requested_at >= ?"
        if real_only:
            sql += " AND dry_run=0"
        return self.conn().execute(sql, (since_iso,)).fetchone()[0]

    # -- APRS gating (stage 4) ------------------------------------------------------------
    def gate_candidates(self, max_position_age_s: float) -> List[Dict[str, Any]]:
        """Stations that have said APRS:Y, are not us, and have a position newer than the
        one last gated (or never gated).  Consent is enforced HERE, not in the UI."""
        cutoff = iso_utc(now_utc() - timedelta(seconds=max_position_age_s))
        return [dict(r) for r in self.conn().execute(
            "SELECT s.callsign, s.base_callsign, s.lat, s.lon, s.grid, s.accuracy_m, s.position_source, s.position_time, "
            "s.last_band, s.aprs_consent_at, g.beacon_id, g.object_name, g.last_sent_at, g.last_lat, g.last_lon, g.sent_count "
            "FROM station s LEFT JOIN aprs_gate g ON g.callsign = s.callsign "
            "WHERE s.aprs_consent = 1 AND s.is_own = 0 AND s.is_hidden = 0 AND s.lat IS NOT NULL "
            "AND s.position_source <> 'aprs' AND s.position_time >= ? "
            "AND (g.last_sent_at IS NULL OR s.position_time > g.last_sent_at) ORDER BY s.position_time DESC", (cutoff,))]

    def gate_get(self, callsign: str) -> Optional[Dict[str, Any]]:
        r = self.conn().execute("SELECT * FROM aprs_gate WHERE callsign=?", (callsign.upper(),)).fetchone()
        return dict(r) if r else None

    def gate_upsert(self, callsign: str, **f: Any) -> None:
        with self._write_lock:
            c = self.conn()
            row = self.gate_get(callsign) or {"callsign": callsign.upper(), "object_name": f.get("object_name") or callsign.upper(),
                                              "beacon_id": None, "last_sent_at": None, "last_lat": None, "last_lon": None,
                                              "sent_count": 0, "last_error": None}
            row.update({k: v for k, v in f.items() if k in row})
            c.execute("INSERT INTO aprs_gate(callsign, object_name, beacon_id, last_sent_at, last_lat, last_lon, sent_count, last_error) "
                      "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(callsign) DO UPDATE SET object_name=excluded.object_name, "
                      "beacon_id=excluded.beacon_id, last_sent_at=excluded.last_sent_at, last_lat=excluded.last_lat, "
                      "last_lon=excluded.last_lon, sent_count=excluded.sent_count, last_error=excluded.last_error",
                      (row["callsign"], row["object_name"], row["beacon_id"], row["last_sent_at"], row["last_lat"],
                       row["last_lon"], row["sent_count"], row["last_error"]))

    def gate_all(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn().execute("SELECT * FROM aprs_gate ORDER BY last_sent_at DESC")]

    def gate_delete(self, callsign: str) -> None:
        with self._write_lock:
            self.conn().execute("DELETE FROM aprs_gate WHERE callsign=?", (callsign.upper(),))

    def gate_sent_since(self, since_iso: str) -> int:
        return self.conn().execute("SELECT COUNT(*) FROM beacon_tx WHERE method='graywolf_object' AND ok=1 AND dry_run=0 "
                                   "AND requested_at >= ?", (since_iso,)).fetchone()[0]

    # -- retention ----------------------------------------------------------------
    def prune(self, keep_days: int, max_positions_per_station: int) -> Dict[str, int]:
        cutoff = iso_utc(now_utc() - timedelta(days=keep_days))
        out = {"positions": 0, "heard": 0, "own": 0}
        with self._write_lock:
            c = self.conn()
            c.execute("BEGIN")
            try:
                # Always keep each station's authoritative position row.
                out["positions"] = c.execute(
                    "DELETE FROM station_position WHERE heard_at < ? AND source_ref NOT IN "
                    "(SELECT position_ref FROM station WHERE position_ref IS NOT NULL)", (cutoff,)).rowcount
                out["positions"] += c.execute(
                    "DELETE FROM station_position WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER "
                    "(PARTITION BY callsign ORDER BY heard_at DESC) AS rn FROM station_position) WHERE rn > ?) "
                    "AND source_ref NOT IN (SELECT position_ref FROM station WHERE position_ref IS NOT NULL)",
                    (max_positions_per_station,)).rowcount
                out["heard"] = c.execute("DELETE FROM station_heard WHERE heard_at < ?", (cutoff,)).rowcount
                out["own"] = c.execute("DELETE FROM own_position WHERE recorded_at < ?", (cutoff,)).rowcount
                c.execute("UPDATE station_position SET raw_json=NULL WHERE heard_at < ? AND raw_json IS NOT NULL",
                          (iso_utc(now_utc() - timedelta(days=30)),))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return out

    def reset_all(self) -> None:
        """Wipe derived data (keeps user notes/favourites is NOT possible; this is a full reset)."""
        with self._write_lock:
            c = self.conn()
            for t in ("station_position", "station_heard", "station", "source_cursor"):
                c.execute(f"DELETE FROM {t}")
