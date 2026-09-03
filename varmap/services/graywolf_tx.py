"""Asking Graywolf to transmit on APRS (stages 3 and 4).

Stage 3  mirror():   after each real VarAC position broadcast, fire VarMap's own
                     APRS position beacon through Graywolf with the same fix.
Stage 4  gate loop:  keep one APRS *object* per consenting VarAC station and
                     re-send it when that station's position changes.

Consent is the law here: a station is gated only while its latest VarAC
broadcast carried APRS:Y (the repository enforces this in gate_candidates),
never our own station, never a station that later sent APRS:N, and never
when the consent is older than gate_consent_max_age_days.  Everything else is
rate-limited, logged to beacon_tx, off by default and dry-run by default.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta
from typing import Any, Dict, Optional

from ..domain.timeparse import iso_utc, now_utc, parse_iso
from ..integration.contracts import OwnFix
from ..integration.graywolf import (VARMAP_MARK, GraywolfError, mirror_beacon_body, object_beacon_body,
                                    object_name_for)

log = logging.getLogger("varmap.graywolf.tx")

GATE_LIMITS = {
    "max_per_hour": 30,              # absolute cap on gated object transmissions per hour (config default 10)
    "min_interval_seconds": 600,     # never re-send the same station's object sooner than this (config default 1800)
    "consent_max_age_days": 90,      # consent must be restated at least this often (config default 30)
}


class GraywolfTx(threading.Thread):
    def __init__(self, rt) -> None:
        super().__init__(name="varmap-graywolf-tx", daemon=True)
        self.rt = rt
        self.cfg = rt.cfg
        self.repo = rt.repo
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.state: Dict[str, Any] = {
            "mirror": {"enabled": False, "dry_run": True, "last": None, "beacon_id": None},
            "gate": {"enabled": False, "dry_run": True, "last_run": None, "sent_last_hour": 0, "candidates": 0,
                     "objects": 0, "last_error": None, "last": None},
        }

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.state, default=str))

    def _client(self):
        g = self.cfg.get("graywolf") or {}
        return self.rt.graywolf.client() if g.get("enabled") else None

    def _log(self, method: str, trigger: str, ok: bool, dry: bool, message: str, lat=None, lon=None, grid=None,
             error: Optional[str] = None) -> None:
        try:
            self.repo.beacon_tx_add(requested_at=iso_utc(now_utc()), sent_at=iso_utc(now_utc()) if ok else None,
                                    lat=lat, lon=lon, grid=grid, trigger=trigger, method=method, message=message,
                                    dry_run=1 if dry else 0, ok=1 if ok else 0, error=error)
        except Exception as e:  # noqa: BLE001
            log.debug("beacon_tx log failed: %s", e)

    # ---------------------------------------------------------------- stage 3
    def mirror(self, fix: OwnFix, trigger: str) -> Dict[str, Any]:
        """Fire VarMap's APRS position beacon in Graywolf with `fix`.  Never raises."""
        g = self.cfg.get("graywolf") or {}
        if not (g.get("enabled") and g.get("mirror_enabled")):
            return {"ok": False, "skipped": "mirror disabled"}
        dry = bool(g.get("dry_run", True))
        mycall = self.rt.vc.mycall()
        if not mycall:
            return {"ok": False, "error": "no callsign"}
        body = mirror_beacon_body(fix, mycall, g)
        summary = f"APRS {body['latitude']},{body['longitude']} {body.get('comment', '')}"
        result: Dict[str, Any] = {"ok": False, "dry_run": dry, "message": summary}
        try:
            if dry:
                result["ok"] = True
            else:
                c = self._client()
                if c is None:
                    raise GraywolfError("Graywolf feed not enabled")
                bid = self._ensure_beacon(c, "graywolf_mirror_beacon_id", VARMAP_MARK + " own", body)
                c.send_beacon(bid)
                result.update(ok=True, beacon_id=bid)
            self._log("graywolf_mirror", f"mirror:{trigger}", True, dry, summary, fix.lat, fix.lon, fix.grid)
            log.info("%s APRS mirror via Graywolf: %s", "DRY-RUN" if dry else "SENT", summary)
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)
            self._log("graywolf_mirror", f"mirror:{trigger}", False, dry, summary, fix.lat, fix.lon, fix.grid, str(e))
            log.warning("APRS mirror failed: %s", e)
        with self._lock:
            self.state["mirror"].update({"enabled": True, "dry_run": dry, "last": result})
        return result

    def _ensure_beacon(self, c, meta_key: str, marker: str, body: Dict[str, Any]) -> int:
        """Update VarMap's beacon in Graywolf, creating it the first time (id remembered in app_meta)."""
        bid = self.repo.meta_get(meta_key)
        if bid:
            try:
                c.update_beacon(int(bid), body)
                return int(bid)
            except GraywolfError as e:
                if "404" not in str(e):
                    raise
        found = c.find_beacon(marker)
        if found and found.get("id") is not None:
            c.update_beacon(int(found["id"]), body)
            self.repo.meta_set(meta_key, str(found["id"]))
            return int(found["id"])
        created = c.create_beacon({**body, "comment": (body.get("comment", "") + " " + marker).strip()[:43]})
        new_id = created.get("id") or (created.get("beacon") or {}).get("id")
        if new_id is None:
            raise GraywolfError(f"Graywolf did not return a beacon id: {created}")
        self.repo.meta_set(meta_key, str(new_id))
        return int(new_id)

    # ---------------------------------------------------------------- stage 4
    def run(self) -> None:
        log.info("Graywolf TX service started (mirror/gate disabled until enabled in settings)")
        while not self._stop.is_set():
            g = self.cfg.get("graywolf") or {}
            with self._lock:
                self.state["mirror"]["enabled"] = bool(g.get("enabled") and g.get("mirror_enabled"))
                self.state["mirror"]["dry_run"] = bool(g.get("dry_run", True))
                self.state["gate"]["enabled"] = bool(g.get("enabled") and g.get("gate_enabled"))
                self.state["gate"]["dry_run"] = bool(g.get("dry_run", True))
            if g.get("enabled") and g.get("gate_enabled"):
                try:
                    self.gate_once()
                except Exception as e:  # noqa: BLE001
                    log.warning("APRS gate run failed: %s", e)
                    with self._lock:
                        self.state["gate"]["last_error"] = str(e)
            self._stop.wait(max(15, int(g.get("gate_run_interval_seconds") or 60)))

    def gate_once(self) -> int:
        g = self.cfg.get("graywolf") or {}
        dry = bool(g.get("dry_run", True))
        mycall = self.rt.vc.mycall()
        now = now_utc()
        per_hour = min(int(g.get("gate_max_per_hour") or 10), GATE_LIMITS["max_per_hour"])
        min_iv = max(int(g.get("gate_min_interval_seconds") or 1800), GATE_LIMITS["min_interval_seconds"])
        consent_days = min(int(g.get("gate_consent_max_age_days") or 30), GATE_LIMITS["consent_max_age_days"])
        max_age = int(g.get("gate_max_position_age_seconds") or 21600)
        sent_hour = self.repo.gate_sent_since(iso_utc(now - timedelta(hours=1)))
        cands = self.repo.gate_candidates(max_age)
        sent = 0
        last: Optional[Dict[str, Any]] = None
        for st in cands:
            if sent_hour + sent >= per_hour:
                break
            consent_at = parse_iso(st.get("aprs_consent_at"))
            if consent_at is None or (now - consent_at) > timedelta(days=consent_days):
                continue  # consent lapsed; the station must restate APRS:Y
            last_sent = parse_iso(st.get("last_sent_at"))
            if last_sent and (now - last_sent).total_seconds() < min_iv:
                continue
            if st.get("last_lat") is not None and abs(st["last_lat"] - st["lat"]) < 1e-6 and abs(st["last_lon"] - st["lon"]) < 1e-6:
                continue  # same position as last time: nothing new to say
            name = object_name_for(st["callsign"])
            if not name:
                self.repo.gate_upsert(st["callsign"], object_name=st["callsign"][:9], last_error="callsign too long for an APRS object")
                continue
            body = object_beacon_body(st, name, mycall, g)
            summary = f"{name} @ {body['latitude']},{body['longitude']} amb{body['ambiguity']} {body['comment']}"
            try:
                bid = st.get("beacon_id")
                if not dry:
                    c = self._client()
                    if c is None:
                        raise GraywolfError("Graywolf feed not enabled")
                    if bid:
                        try:
                            c.update_beacon(int(bid), body)
                        except GraywolfError as e:
                            if "404" not in str(e):
                                raise
                            bid = None
                    if not bid:
                        found = c.find_beacon(name)
                        if found and found.get("id") is not None:
                            bid = int(found["id"])
                            c.update_beacon(bid, body)
                        else:
                            created = c.create_beacon(body)
                            bid = created.get("id") or (created.get("beacon") or {}).get("id")
                            if bid is None:
                                raise GraywolfError(f"no beacon id returned: {created}")
                    c.send_beacon(int(bid))
                self.repo.gate_upsert(st["callsign"], object_name=name, beacon_id=bid, last_sent_at=iso_utc(now),
                                      last_lat=st["lat"], last_lon=st["lon"], sent_count=int(st.get("sent_count") or 0) + 1,
                                      last_error=None)
                self._log("graywolf_object", f"gate:{st['callsign']}", True, dry, summary, st["lat"], st["lon"], st.get("grid"))
                log.info("%s APRS object via Graywolf: %s", "DRY-RUN" if dry else "SENT", summary)
                sent += 1
                last = {"callsign": st["callsign"], "summary": summary, "dry_run": dry, "ok": True, "at": iso_utc(now)}
            except Exception as e:  # noqa: BLE001
                self.repo.gate_upsert(st["callsign"], object_name=name, last_error=str(e))
                self._log("graywolf_object", f"gate:{st['callsign']}", False, dry, summary, st["lat"], st["lon"], st.get("grid"), str(e))
                log.warning("APRS object for %s failed: %s", st["callsign"], e)
                last = {"callsign": st["callsign"], "summary": summary, "dry_run": dry, "ok": False, "error": str(e), "at": iso_utc(now)}
        self._retire(g, dry)
        with self._lock:
            self.state["gate"].update({"last_run": iso_utc(now), "sent_last_hour": sent_hour + sent, "candidates": len(cands),
                                       "objects": len(self.repo.gate_all()), "last_error": None if last is None or last.get("ok") else last.get("error"),
                                       "last": last or self.state["gate"].get("last")})
        return sent

    def _retire(self, g: Dict[str, Any], dry: bool) -> None:
        """Forget objects for stations silent longer than gate_retire_hours (delete the Graywolf beacon)."""
        hours = float(g.get("gate_retire_hours") or 24)
        cutoff = now_utc() - timedelta(hours=hours)
        for row in self.repo.gate_all():
            st = self.repo.station(row["callsign"])
            last_heard = parse_iso(st.get("last_heard")) if st else None
            if last_heard and last_heard > cutoff:
                continue
            try:
                if row.get("beacon_id") and not dry:
                    c = self._client()
                    if c is not None:
                        c.delete_beacon(int(row["beacon_id"]))
                self.repo.gate_delete(row["callsign"])
                log.info("APRS object %s retired (silent > %.0f h)", row["object_name"], hours)
            except Exception as e:  # noqa: BLE001
                log.debug("retire %s failed: %s", row["callsign"], e)
