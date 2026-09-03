"""Transmit scheduler + safety interlocks.  Decides WHEN (policy) and applies
the interlocks; the HOW lives in integration/varac_tx.py.

Interlocks (design doc 7.7), all enforced here regardless of policy:
 1. master switch, default OFF        5. fix-age limit
 2. hard minimum interval (policy)    6. independent per-hour rate limiter
 3. own callsign required             7. every request/outcome logged
 4. never without a position fix      8. kill switch (disable) always available
 9. never drive the GUI when VarAC is not running
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta
from typing import Any, Dict, Optional

from ..domain.gpstag import APRS_CONSENT_NO, APRS_CONSENT_YES, format_gps_tag
from ..domain.smartbeacon import Fix, make_policy
from ..domain.timeparse import iso_utc, now_utc, parse_iso
from ..integration import varac_tx
from ..integration.contracts import OwnFix

log = logging.getLogger("varmap.beacon")
TICK_SECONDS = 2.0
FAIL_BACKOFF_SECONDS = 120.0   # after a failed hand-over to VarAC, do not try again sooner than this

# Anti-spam limits that NO configuration can relax.  VarAC broadcasts go out on
# shared calling frequencies; a position beacon every few minutes from a parked
# station is noise for everyone.  (Values are seconds unless stated.)
LIMITS = {
    "min_interval_seconds": 300,       # never two transmissions closer than 5 min, any mode
    "min_keepalive_seconds": 1800,     # a station that has not moved repeats itself at most every 30 min
    "min_move_m": 100.0,               # "moved" means at least 100 m
    "max_per_hour": 6,                 # absolute hourly cap (config default 2)
    "max_per_day": 48,                 # absolute daily cap
}


def clamp_beacon_config(bc: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the beacon config with the anti-spam limits applied."""
    out = json.loads(json.dumps(bc))
    L = LIMITS
    f = out.setdefault("fixed", {})
    f["interval_seconds"] = max(float(f.get("interval_seconds", 900)), L["min_interval_seconds"])
    f["min_move_m"] = max(float(f.get("min_move_m", 500)), L["min_move_m"])
    f["max_interval_seconds"] = max(float(f.get("max_interval_seconds", 3600)), L["min_keepalive_seconds"],
                                    f["interval_seconds"])
    s = out.setdefault("smart", {})
    s["min_interval_seconds"] = max(float(s.get("min_interval_seconds", 300)), L["min_interval_seconds"])
    s["fast_rate_seconds"] = max(float(s.get("fast_rate_seconds", 300)), s["min_interval_seconds"])
    s["slow_rate_seconds"] = max(float(s.get("slow_rate_seconds", 1800)), L["min_keepalive_seconds"])
    s["max_interval_seconds"] = max(float(s.get("max_interval_seconds", 3600)), L["min_keepalive_seconds"])
    s["min_move_m"] = max(float(s.get("min_move_m", 500)), L["min_move_m"])
    s["min_turn_time_seconds"] = max(float(s.get("min_turn_time_seconds", 60)), L["min_interval_seconds"])
    out["max_per_hour"] = int(min(max(int(out.get("max_per_hour", 2)), 1), L["max_per_hour"]))
    out["max_per_day"] = L["max_per_day"]
    return out


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


class BeaconService(threading.Thread):
    def __init__(self, rt) -> None:
        super().__init__(name="varmap-beacon", daemon=True)
        self.rt = rt
        self.cfg = rt.cfg
        self.repo = rt.repo
        self.vc = rt.vc
        self.tracker = rt.tracker
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._policy = None
        self._policy_key: Optional[str] = None
        self._dcd_state: Optional[bool] = None
        self._dcd_checked_at = 0.0
        self._activity: Dict[str, Any] = {"running": False, "busy": None, "reason": None}
        self._activity_at = 0.0
        self._last_fail_at = 0.0
        self._last_fail_reason = ""
        self._cf: Optional[int] = None
        self._cf_checked_at = 0.0
        self.state: Dict[str, Any] = {
            "enabled": False, "dry_run": True, "mode": "fixed", "method": "broadcast",
            "blocked": None, "decision": None, "next_due_seconds": None,
            "last_tx": None, "tx_last_hour": 0, "last_tick": None, "ignore_dcd": None, "varac_activity": None,
            "on_calling_frequency_hz": None, "current_frequency_hz": None,
        }

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.state, default=str))

    # -- policy management ---------------------------------------------------
    def _policy_for(self, bc: Dict[str, Any]):
        mode = (bc.get("mode") or "fixed").lower()
        pcfg = bc.get("smart" if mode == "smart" else "fixed") or {}
        key = json.dumps({"mode": mode, "cfg": pcfg}, sort_keys=True)
        if self._policy is None or key != self._policy_key:
            last = self._policy.last_tx if self._policy else None
            self._policy = make_policy(mode, pcfg)
            if last is not None:
                self._policy.mark_sent(last, last.time)
            self._policy_key = key
        return self._policy

    def reset_schedule(self) -> None:
        with self._lock:
            if self._policy:
                self._policy.reset()

    # -- message -------------------------------------------------------------
    def build_message(self, fix: OwnFix, bc: Optional[Dict[str, Any]] = None) -> str:
        bc = bc or self.cfg.get("beacon")
        dec = int(bc.get("coord_decimals") or 5)
        fields = _SafeDict(
            gpstag=format_gps_tag(fix.lat, fix.lon, dec),
            lat=f"{fix.lat:.{dec}f}", lon=f"{fix.lon:.{dec}f}",
            grid=fix.grid or "", call=self.vc.mycall(),
            speed_kmh=f"{fix.speed_kmh:.0f}" if fix.speed_kmh is not None else "",
            speed_mph=f"{fix.speed_kmh / 1.609344:.0f}" if fix.speed_kmh is not None else "",
            course=f"{fix.course_deg:.0f}" if fix.course_deg is not None else "",
            alt=f"{fix.altitude_m:.0f}" if fix.altitude_m is not None else "",
            comment=bc.get("comment") or "",
            time=now_utc().strftime("%H%Mz"),
            aprsflag=APRS_CONSENT_YES if bc.get("aprs_consent") else "",
        )
        tpl = bc.get("message_template") or "{gpstag} {grid} {comment} {aprsflag}"
        if bc.get("aprs_consent") and "{aprsflag}" not in tpl:
            tpl += " {aprsflag}"   # consent must reach the air even with an older custom template
        msg = " ".join(tpl.format_map(fields).split())
        return msg[:varac_tx.BROADCAST_MAX_BYTES]

    def relay_message(self, station: Dict[str, Any], comment: str = "") -> str:
        """Stage 5: an APRS station's position as a VarAC broadcast, e.g.
        'APRS KK4ODA-9 <GPS:33.86000,-84.30000> via KK4ODA'."""
        dec = int((self.cfg.get("beacon") or {}).get("coord_decimals") or 5)
        parts = ["APRS", station["callsign"], format_gps_tag(float(station["lat"]), float(station["lon"]), dec)]
        if comment:
            parts.append(comment)
        parts.append(f"via {self.vc.mycall()}")
        msg = " ".join(" ".join(parts).split())
        return msg[:varac_tx.BROADCAST_MAX_BYTES]

    def preview(self) -> Dict[str, Any]:
        fix = self.tracker.current()
        if not fix:
            return {"ok": False, "error": "no position fix", "message": None}
        msg = self.build_message(fix)
        return {"ok": True, "message": msg, "length": len(msg), "max": varac_tx.BROADCAST_MAX_BYTES}

    # -- interlocks -------------------------------------------------------------
    def _blocked_reason(self, bc: Dict[str, Any], fix: Optional[OwnFix]) -> Optional[str]:
        if not self.vc.mycall():
            return "no callsign configured (set Mycall in VarAC or own_station.callsign)"
        if fix is None:
            return "no position fix"
        max_age = float(bc.get("max_fix_age_seconds") or 900)
        age = (now_utc() - fix.time).total_seconds()
        if age > max_age:
            return f"position fix is {int(age)} s old (limit {int(max_age)} s)"
        per_hour = int(bc.get("max_per_hour") or 6)
        real_only = not bc.get("dry_run")
        n = self.repo.beacon_tx_count_since(iso_utc(now_utc() - timedelta(hours=1)), real_only=real_only)
        if n >= per_hour:
            return f"rate limit: {n} transmissions in the last hour (max {per_hour})"
        nd = self.repo.beacon_tx_count_since(iso_utc(now_utc() - timedelta(hours=24)), real_only=real_only)
        if nd >= int(bc.get("max_per_day") or LIMITS["max_per_day"]):
            return f"daily limit: {nd} transmissions in the last 24 h (max {bc.get('max_per_day')})"
        last = self.repo.beacon_tx_recent(limit=1)
        if last and last[0].get("ok") and (not last[0].get("dry_run") or not real_only):
            since = (now_utc() - (parse_iso(last[0]["requested_at"]) or now_utc())).total_seconds()
            if since < LIMITS["min_interval_seconds"]:
                return f"minimum spacing: last transmission {int(since)} s ago (floor {LIMITS['min_interval_seconds']} s)"
        if (bc.get("mode") or "fixed").lower() == "smart":
            # Smart timing is meant for a QSY'd tracker frequency, never for the shared calling frequencies.
            cf = self._on_calling_frequency(bc)
            if cf:
                return (f"smart timing is not allowed on calling frequency {cf / 1e6:.3f} MHz; "
                        "use Fixed interval here, or QSY off the calling frequency")
        if not bc.get("dry_run"):
            running = self.rt.poller.snapshot().get("varac_running")
            if running is False:
                return "VarAC is not running"
            act = self._varac_activity()
            if act.get("running") and act.get("busy"):
                return f"holding: {act.get('reason')}"
            if bc.get("dcd_guard", True):
                if self._ignore_dcd():
                    return "VarAC is set to Ignore DCD (busy-channel protection is off); untick it in VarAC or disable the DCD guard"
            since_fail = time.time() - self._last_fail_at
            if since_fail < FAIL_BACKOFF_SECONDS:
                return f"last attempt failed ({self._last_fail_reason}); retrying in {int(FAIL_BACKOFF_SECONDS - since_fail)} s"
        return None

    def _on_calling_frequency(self, bc: Dict[str, Any]) -> Optional[int]:
        """Cached (5 s): the VarAC calling frequency we are currently on, or None."""
        now = time.time()
        if now - self._cf_checked_at > 5.0:
            try:
                self._cf = self.vc.on_calling_frequency(int(bc.get("cf_window_hz") or 3000))
            except Exception as e:  # pragma: no cover
                log.debug("calling-frequency check failed: %s", e)
                self._cf = None
            self._cf_checked_at = now
            with self._lock:
                self.state["on_calling_frequency_hz"] = self._cf
                self.state["current_frequency_hz"] = self.vc.current_frequency_hz()
        return self._cf

    def _varac_activity(self) -> Dict[str, Any]:
        """Cached (5 s) read of VarAC's button states: connected / dialog open."""
        now = time.time()
        if now - self._activity_at > 5.0:
            try:
                self._activity = varac_tx.varac_activity()
            except Exception as e:  # pragma: no cover
                log.debug("VarAC activity read failed: %s", e)
                self._activity = {"running": False, "busy": None, "reason": str(e)}
            self._activity_at = now
            with self._lock:
                self.state["varac_activity"] = self._activity
        return self._activity

    def _ignore_dcd(self) -> Optional[bool]:
        """Cached read of VarAC's 'Ignore DCD' checkbox (window enumeration every tick is wasteful)."""
        now = time.time()
        if now - self._dcd_checked_at > 5.0:
            try:
                self._dcd_state = varac_tx.ignore_dcd_state()
            except Exception as e:  # pragma: no cover
                log.debug("ignore-DCD read failed: %s", e)
                self._dcd_state = None
            self._dcd_checked_at = now
            with self._lock:
                self.state["ignore_dcd"] = self._dcd_state
        return self._dcd_state

    # -- loop --------------------------------------------------------------------
    def run(self) -> None:
        log.info("beacon service started (disabled until enabled in settings)")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # pragma: no cover
                log.exception("beacon tick failed: %s", e)
            self._stop.wait(TICK_SECONDS)

    def tick(self) -> None:
        bc = clamp_beacon_config(self.cfg.get("beacon"))
        fix = self.tracker.current()
        with self._lock:
            self.state.update({"enabled": bool(bc.get("enabled")), "dry_run": bool(bc.get("dry_run")),
                               "mode": bc.get("mode"), "method": bc.get("method"), "limits": dict(LIMITS),
                               "effective": {"fixed": bc["fixed"], "smart": bc["smart"], "max_per_hour": bc["max_per_hour"]},
                               "last_tick": iso_utc(now_utc()),
                               "tx_last_hour": self.repo.beacon_tx_count_since(
                                   iso_utc(now_utc() - timedelta(hours=1)), real_only=False)})
            if not bc.get("enabled"):
                self.state.update({"blocked": None, "decision": "disabled", "next_due_seconds": None})
                return
            blocked = self._blocked_reason(bc, fix)
            if blocked:
                self.state.update({"blocked": blocked, "decision": "blocked", "next_due_seconds": None})
                return
            policy = self._policy_for(bc)
            pfix = Fix(fix.lat, fix.lon, fix.time, fix.speed_kmh, fix.course_deg)
            d = policy.evaluate(pfix, now_utc())
            self.state.update({"blocked": None, "decision": d.reason, "next_due_seconds": d.next_due_seconds})
            if d.send:
                self._transmit(bc, fix, d.reason)

    # -- transmit ------------------------------------------------------------------
    def _transmit(self, bc: Dict[str, Any], fix: OwnFix, trigger: str, message: Optional[str] = None) -> Dict[str, Any]:
        method = (bc.get("method") or "broadcast").lower()
        dry = bool(bc.get("dry_run"))
        if message is None:
            message = self.build_message(fix, bc) if method == "broadcast" else f"(one-time advanced beacon: grid {fix.grid})"
        else:
            method = "broadcast"   # relayed text always goes out as a broadcast
        requested = iso_utc(now_utc())
        ok, err = True, None
        if not dry:
            if method == "beacon":
                ok, err = varac_tx.send_one_time_beacon()
            else:
                ok, err = varac_tx.send_broadcast(message, bc.get("broadcast_to") or "ALL")
        try:
            freq = self.vc.current_frequency_hz()
        except Exception:
            freq = None
        row = dict(requested_at=requested, sent_at=iso_utc(now_utc()) if ok else None, lat=fix.lat, lon=fix.lon,
                   grid=fix.grid, trigger=trigger, method=method, message=message, dry_run=1 if dry else 0,
                   ok=1 if ok else 0, error=err or None, frequency_hz=freq)
        try:
            self.repo.beacon_tx_add(**row)
        except Exception as e:
            log.warning("could not log beacon_tx: %s", e)
        if ok:
            policy = self._policy_for(bc)
            policy.mark_sent(Fix(fix.lat, fix.lon, fix.time, fix.speed_kmh, fix.course_deg), now_utc())
            self._last_fail_at = 0.0
            log.info("%s %s via %s: %s", "DRY-RUN" if dry else "SENT", trigger, method, message)
            if not dry and not trigger.startswith("relay"):
                try:   # stage 3: mirror our own position to APRS through Graywolf (no-op unless enabled)
                    gtx = getattr(self.rt, "graywolf_tx", None)
                    if gtx is not None:
                        gtx.mirror(fix, trigger)
                except Exception as e:  # noqa: BLE001
                    log.debug("APRS mirror hook failed: %s", e)
        else:
            self._last_fail_at = time.time()
            self._last_fail_reason = err or "unknown"
            self._activity_at = 0.0   # re-read VarAC's state on the next tick
            log.warning("transmit failed (%s via %s): %s", trigger, method, err)
        with self._lock:
            self.state["last_tx"] = row
        return row

    def rehearse(self) -> Dict[str, Any]:
        """Fill VarAC's broadcast dialog with the real message and close it WITHOUT sending."""
        fix = self.tracker.current()
        if not fix:
            return {"ok": False, "error": "no position fix"}
        bc = self.cfg.get("beacon")
        msg = self.build_message(fix, bc)
        r = varac_tx.rehearse_broadcast(msg, bc.get("broadcast_to") or "ALL")
        r["message"] = msg
        log.info("broadcast rehearsal: ok=%s typed=%s err=%s", r.get("ok"), r.get("typed_bytes"), r.get("error"))
        return r

    def send_now(self) -> Dict[str, Any]:
        """Manual trigger: honours every interlock (including the 5-minute spacing) except the policy's schedule."""
        bc = clamp_beacon_config(self.cfg.get("beacon"))
        fix = self.tracker.current()
        with self._lock:
            blocked = self._blocked_reason(bc, fix)
            if blocked:
                return {"ok": False, "error": blocked}
            row = self._transmit(bc, fix, "manual")
            return {"ok": bool(row["ok"]), "error": row.get("error"), "message": row.get("message"),
                    "dry_run": bool(row.get("dry_run"))}

    def relay_to_varac(self, station: Dict[str, Any], comment: str = "") -> Dict[str, Any]:
        """Stage 5: broadcast an APRS station's position on VarAC.  Manual, one-off,
        subject to every interlock and to the same hourly cap as our own broadcasts."""
        if not station or station.get("lat") is None:
            return {"ok": False, "error": "station has no position"}
        bc = clamp_beacon_config(self.cfg.get("beacon"))
        fix = self.tracker.current()
        with self._lock:
            blocked = self._blocked_reason(bc, fix)
            if blocked:
                return {"ok": False, "error": blocked}
            msg = self.relay_message(station, comment)
            row = self._transmit(bc, fix, f"relay:{station['callsign']}", message=msg)
            return {"ok": bool(row["ok"]), "error": row.get("error"), "message": msg, "dry_run": bool(row.get("dry_run"))}

    def set_enabled(self, enabled: bool, dry_run: Optional[bool] = None) -> None:
        patch: Dict[str, Any] = {"beacon": {"enabled": bool(enabled)}}
        if dry_run is not None:
            patch["beacon"]["dry_run"] = bool(dry_run)
        self.cfg.update(patch)
        if not enabled:
            self.reset_schedule()
        log.info("beacon %s (dry_run=%s)", "ENABLED" if enabled else "DISABLED", self.cfg.get("beacon", "dry_run"))
