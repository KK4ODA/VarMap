"""The ingest loop: polls VarAC's database read-only by high-water mark and
feeds observations into our repository.  Never lets a source failure kill
the loop (HamLink's discipline); backs off on repeated errors.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import timedelta
from typing import Any, Callable, Dict, Optional

from ..domain.timeparse import iso_utc, now_utc
from ..integration.varac_db import IncompatibleSchema, NotAVaracDatabase, VaracDbSource
from ..integration.varac_tx import is_varac_running

log = logging.getLogger("varmap.poller")

BACKOFF_STEPS = [5, 10, 30, 60, 300]


class Poller(threading.Thread):
    def __init__(self, rt) -> None:
        super().__init__(name="varmap-poller", daemon=True)
        self.rt = rt
        self.cfg = rt.cfg
        self.repo = rt.repo
        self.vc = rt.vc
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._backoff_idx = -1
        self._last_contacts = 0.0
        self._last_running_check = 0.0
        self.state: Dict[str, Any] = {
            "status": "starting", "connected": False, "db_path": None, "db_version": None,
            "identity": None, "last_poll": None, "last_ok": None, "last_error": None,
            "error_count": 0, "backoff_seconds": 0, "frames_total": 0, "last_batch": None,
            "backfill": None, "varac_running": None, "poll_ms": None, "probe": None,
        }

    # -- control -------------------------------------------------------------
    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self.state.update(kw)

    # -- loop ----------------------------------------------------------------
    def run(self) -> None:
        log.info("poller started")
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # pragma: no cover - last-resort guard
                log.exception("poll failed unexpectedly: %s", e)
                self._fail(str(e))
            iv = max(2, int(self.cfg.get("varac", "poll_interval_seconds") or 10))
            wait = BACKOFF_STEPS[self._backoff_idx] if self._backoff_idx >= 0 else iv
            self._set(backoff_seconds=wait if self._backoff_idx >= 0 else 0)
            self._wake.wait(wait)
            self._wake.clear()
        log.info("poller stopped")

    def _fail(self, error: str, transient: bool = False) -> None:
        if not transient:
            self._backoff_idx = min(self._backoff_idx + 1, len(BACKOFF_STEPS) - 1)
        with self._lock:
            self.state["connected"] = False
            self.state["status"] = "locked" if transient else "error"
            self.state["last_error"] = error
            self.state["error_count"] += 1
            self.state["last_poll"] = iso_utc(now_utc())
        (log.debug if transient else log.warning)("poll: %s", error)

    def _check_varac_running(self) -> None:
        if time.time() - self._last_running_check > 30:
            self._last_running_check = time.time()
            self._set(varac_running=is_varac_running())

    # -- one poll --------------------------------------------------------------
    def poll_once(self) -> None:
        t0 = time.time()
        self._check_varac_running()
        path = self.vc.db_path()
        self._set(db_path=path)
        if not path or not os.path.isfile(path):
            self._fail(f"VarAC database not found: {path or '(no path)'}")
            return
        src = VaracDbSource(path, self.vc.mycall())
        try:
            conn = src.open()
        except sqlite3.OperationalError as e:
            self._fail(f"cannot open database: {e}", transient="locked" in str(e).lower())
            return
        try:
            try:
                probe = src.probe(conn)
            except (NotAVaracDatabase, IncompatibleSchema) as e:
                self._fail(str(e))
                return
            self._note_identity(src, probe)
            max_ids = src.max_ids(conn)
            total = 0
            total += self._drain(conn, src, "cqframe", max_ids["cqframe"], "cqframe_time",
                                 src.fetch_cqframes, src.cqframe_to_observation)
            if self.cfg.get("varac", "scan_broadcasts") and src.has_broadcast:
                total += self._drain(conn, src, "broadcast", max_ids.get("broadcast", 0), "broadcast_time",
                                     src.fetch_broadcasts, src.broadcast_to_observation)
            if self.cfg.get("varac", "scan_vmail_gps_tags") and src.has_vmail:
                total += self._drain(conn, src, "vmail", max_ids.get("vmail", 0), "creation_time",
                                     src.fetch_vmails, src.vmail_to_observation)
            if time.time() - self._last_contacts > 3600 and src.has_contact:
                self._last_contacts = time.time()
                try:
                    n = self.repo.enrich_from_contacts(src.fetch_contacts(conn))
                    if n:
                        log.info("enriched %d stations from VarAC contacts", n)
                except Exception as e:
                    log.debug("contact enrichment failed: %s", e)
            self._backoff_idx = -1
            now = iso_utc(now_utc())
            with self._lock:
                self.state.update({"status": "ok", "connected": True, "last_poll": now, "last_ok": now,
                                   "last_error": None, "last_batch": total,
                                   "frames_total": self.state["frames_total"] + total,
                                   "poll_ms": int((time.time() - t0) * 1000), "max_ids": max_ids})
            if total:
                self.rt.on_new_data(total)
        except sqlite3.OperationalError as e:
            self._fail(f"database busy: {e}", transient="locked" in str(e).lower() or "busy" in str(e).lower())
        finally:
            conn.close()

    def _note_identity(self, src: VaracDbSource, probe: Dict[str, Any]) -> None:
        prev_id = self.repo.meta_get("varac_identity")
        prev_ver = self.repo.meta_get("varac_db_version")
        if prev_id and prev_id != src.identity:
            log.warning("VarAC database identity changed (%s -> %s): restore, new install or new path. "
                        "Cursors are per-database; a fresh backfill will follow.", prev_id, src.identity)
        if prev_ver and prev_ver != str(src.db_version):
            log.warning("VarAC database upgraded (db_version %s -> %s); schema re-probed.", prev_ver, src.db_version)
        if prev_id != src.identity:
            self.repo.meta_set("varac_identity", src.identity)
        if prev_ver != str(src.db_version):
            self.repo.meta_set("varac_db_version", str(src.db_version))
        self._set(db_version=src.db_version, identity=src.identity, probe=probe)

    def _initial_cursor(self, conn, src: VaracDbSource, table: str, max_id: int, time_col: str) -> int:
        policy = (self.cfg.get("varac", "backfill") or "all").strip().lower()
        if policy == "none":
            return max_id
        if policy.startswith("days:"):
            try:
                days = float(policy.split(":", 1)[1])
            except ValueError:
                days = 30.0
            cutoff = (now_utc() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            first = src.min_id_since(conn, table, time_col, cutoff)
            return max(first - 1, 0) if first else max_id
        return 0

    def _drain(self, conn, src: VaracDbSource, table: str, max_id: int, time_col: str,
               fetch: Callable, mapper: Callable) -> int:
        sid = f"{table}@{src.identity}"
        rec = self.repo.cursor_get(sid)
        if rec is None:
            hwm = self._initial_cursor(conn, src, table, max_id, time_col)
            self.repo.cursor_set(sid, str(hwm))
            log.info("first run for %s: starting at id %d of %d", table, hwm, max_id)
        else:
            hwm = int(rec["cursor"] or 0)
        if hwm > max_id:
            log.warning("%s shrank (cursor %d > max id %d): restored from backup? Resetting cursor.", table, hwm, max_id)
            hwm = max_id
            self.repo.cursor_set(sid, str(hwm))
        batch = max(100, int(self.cfg.get("varac", "batch_size") or 2000))
        read_cq = bool(self.cfg.get("varac", "read_cq_frames"))
        backlog = max_id - hwm
        if backlog > batch:
            self._set(backfill={"table": table, "total": backlog, "done": 0, "active": True})
        total = 0
        while not self._stop.is_set():
            rows = fetch(conn, hwm, batch)
            if not rows:
                break
            obs = []
            for r in rows:
                try:
                    o = mapper(r)
                except Exception as e:
                    log.warning("row %s/%s unparseable: %s", table, r["id"], e)
                    o = None
                if o is None:
                    continue
                if table == "cqframe" and o.source == "cq" and not read_cq:
                    continue
                obs.append(o)
            hwm = int(rows[-1]["id"])
            stats = self.repo.ingest(obs, {sid: str(hwm)})
            total += len(rows)
            if stats.errors:
                log.warning("ingest %s: %d row errors, first: %s", table, len(stats.errors), stats.errors[0])
            if backlog > batch:
                self._set(backfill={"table": table, "total": backlog, "done": total, "active": True})
            if len(rows) < batch:
                break
        if backlog > batch:
            self._set(backfill={"table": table, "total": backlog, "done": total, "active": False})
        return total
