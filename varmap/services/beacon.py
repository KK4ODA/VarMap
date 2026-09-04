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
import re
import statistics
import threading
import time
from collections import deque
from datetime import timedelta
from typing import Any, Dict, Optional

from ..domain.geo import freq_to_band, normalise_band
from ..domain.gpstag import APRS_CONSENT_NO, APRS_CONSENT_YES, format_gps_tag
from ..domain.smartbeacon import Fix, make_policy
from ..domain.timeparse import iso_utc, now_utc, parse_iso
from ..integration import varac_tx
from ..integration.contracts import OwnFix

log = logging.getLogger("varmap.beacon")
TICK_SECONDS = 2.0
FAIL_BACKOFF_SECONDS = 120.0   # first retry after a failed hand-over to VarAC; doubles per failure
FAIL_BACKOFF_MAX_SECONDS = 1800.0
SCANNER_DWELL_MAX_SECONDS = 600.0   # a frequency held longer than this is a QSY, not a scanner stop
VERIFY_TX_SECONDS = 40.0            # how long to wait for VarAC.log to show the real TX frequency

# Anti-spam limits that NO configuration can relax.  VarAC broadcasts go out on
# shared calling frequencies; a position beacon every few minutes from a parked
# station is noise for everyone.  (Values are seconds unless stated.)
LIMITS = {
    "min_interval_seconds": 300,       # automatic transmissions: never closer than 5 min
    "manual_min_spacing_seconds": 60,  # manual buttons (Send once now, Relay to VarAC): 60 s, guards against double clicks
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
    ka = float(f.get("max_interval_seconds", 3600))
    f["max_interval_seconds"] = 0.0 if ka <= 0 else max(ka, L["min_keepalive_seconds"], f["interval_seconds"])
    s = out.setdefault("smart", {})
    s["min_interval_seconds"] = max(float(s.get("min_interval_seconds", 300)), L["min_interval_seconds"])
    s["fast_rate_seconds"] = max(float(s.get("fast_rate_seconds", 300)), s["min_interval_seconds"])
    s["slow_rate_seconds"] = max(float(s.get("slow_rate_seconds", 1800)), L["min_keepalive_seconds"])
    sk = float(s.get("max_interval_seconds", 3600))
    s["max_interval_seconds"] = 0.0 if sk <= 0 else max(sk, L["min_keepalive_seconds"])
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
        self._pending: Optional[Dict[str, Any]] = None   # a manual send waiting for a preferred band
        self._freq_hz: Optional[int] = None               # VarAC's frequency as last observed by us
        self._freq_changed_at = 0.0                        # when it last changed (our clock)
        self._dwells: deque = deque(maxlen=8)              # recent scanner stop lengths (s)
        self._fail_streak = 0
        self._block_logged: Optional[str] = None
        self._verify: Optional[Dict[str, Any]] = None      # a sent row whose real TX frequency we still owe
        self.state: Dict[str, Any] = {
            "enabled": False, "dry_run": True, "mode": "fixed", "method": "broadcast",
            "blocked": None, "decision": None, "next_due_seconds": None,
            "last_tx": None, "tx_last_hour": 0, "last_tick": None, "ignore_dcd": None, "varac_activity": None,
            "on_calling_frequency_hz": None, "current_frequency_hz": None,
            "current_band": None, "preferred_bands": [], "holding_for_band": None, "pending": None,
            "scanner_dwell_seconds": None, "band_window_seconds": None, "band_age_seconds": None,
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
    def _blocked_reason(self, bc: Dict[str, Any], fix: Optional[OwnFix], manual: bool = False) -> Optional[str]:
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
        # Spacing from the previous VarAC transmission of any kind.  Automatic sends keep the
        # 5-minute floor; a manual button press is the operator's own call and only needs a
        # short guard against double clicks (VarAC still enforces its busy-channel wait).
        floor = LIMITS["manual_min_spacing_seconds"] if manual else LIMITS["min_interval_seconds"]
        last = next((r for r in self.repo.beacon_tx_recent(limit=10) if str(r.get("method", "")) in ("broadcast", "beacon")), None)
        if last and last.get("ok") and (not last.get("dry_run") or not real_only):
            since = (now_utc() - (parse_iso(last["requested_at"]) or now_utc())).total_seconds()
            if since < floor:
                return f"minimum spacing: last VarAC transmission {int(since)} s ago (floor {int(floor)} s{' for manual sends' if manual else ''})"
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
            if varac_tx.session_locked():
                return "Windows session is locked; VarMap cannot drive VarAC's window until you unlock"
            act = self._varac_activity()
            if act.get("running") and act.get("busy"):
                return f"holding: {act.get('reason')}"
            if bc.get("dcd_guard", True):
                if self._ignore_dcd():
                    return "VarAC is set to Ignore DCD (busy-channel protection is off); untick it in VarAC or disable the DCD guard"
            since_fail = time.time() - self._last_fail_at
            backoff = self._fail_backoff()
            if since_fail < backoff:
                return (f"last attempt failed ({self._last_fail_reason}); {self._fail_streak} in a row, "
                        f"retrying in {int(backoff - since_fail)} s")
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

    # -- preferred bands (the scanner decides the frequency; we decide the moment) ------
    def _preferred_bands(self, bc: Dict[str, Any]) -> list:
        raw = bc.get("preferred_bands") or []
        if isinstance(raw, str):
            raw = raw.split(",")
        return [normalise_band(b) for b in raw if normalise_band(b)]

    def _fail_backoff(self) -> float:
        """120 s after the first failure, doubling each time, capped at 30 min: a locked
        or wedged VarAC is not poked every two minutes all night."""
        return min(FAIL_BACKOFF_SECONDS * (2 ** max(0, self._fail_streak - 1)), FAIL_BACKOFF_MAX_SECONDS)

    def _note_block(self, reason: Optional[str]) -> None:
        """Log block reasons once per change (numbers normalised), so a silent night is explained."""
        key = re.sub(r"\d+", "#", reason) if reason else None
        if key != self._block_logged:
            self._block_logged = key
            if reason:
                log.info("position TX held: %s", reason)
            else:
                log.info("position TX no longer held")

    def _observe_frequency(self) -> Optional[int]:
        """Track VarAC's frequency on our own clock so we know how long it has been on the
        current band and how long the scanner tends to stay on each stop."""
        try:
            hz = self.vc.current_frequency_hz()
        except Exception:  # noqa: BLE001
            hz = None
        now = time.time()
        if hz != self._freq_hz:
            if self._freq_hz is not None and self._freq_changed_at:
                stay = now - self._freq_changed_at
                if stay <= SCANNER_DWELL_MAX_SECONDS:
                    self._dwells.append(stay)
            self._freq_hz, self._freq_changed_at = hz, now
        return hz

    def _scanner_dwell(self) -> Optional[float]:
        """Typical scanner stop length, or None when the scanner does not look active."""
        if len(self._dwells) < 2 or time.time() - self._freq_changed_at > SCANNER_DWELL_MAX_SECONDS:
            return None
        return float(statistics.median(self._dwells))

    def _band_window(self, dwell: float) -> float:
        return max(4.0, min(0.35 * dwell, 15.0))

    def _current_band(self) -> Optional[str]:
        return freq_to_band(self._observe_frequency())

    def _band_hold(self, bc: Dict[str, Any]) -> Optional[str]:
        """None when VarAC is on an acceptable band, else a human reason to wait."""
        pref = self._preferred_bands(bc)
        if not pref:
            return None
        band = self._current_band()
        if band not in pref:
            return f"waiting for VarAC to be on {'/'.join(pref)} (now {band or 'unknown'})"
        # On a preferred band.  If the scanner is hopping, only start a send early in the
        # stop: the dialog takes a few seconds and VarAC queues the broadcast until the
        # channel is clear, so a late start goes out on the NEXT band (seen live, 2026-09-04).
        dwell = self._scanner_dwell()
        if dwell:
            window = self._band_window(dwell)
            age = time.time() - self._freq_changed_at
            if age > window:
                return (f"on {band} but {int(age)} s into a ~{int(dwell)} s scanner stop (sends start within "
                        f"{int(window)} s); waiting for the next {band} stop")
        return None

    def _band_precheck(self, bc: Dict[str, Any]):
        """Callable for varac_tx: re-check the band right before the final click."""
        pref = self._preferred_bands(bc)
        if not pref:
            return None

        def check() -> Optional[str]:
            band = freq_to_band(self.vc.current_frequency_hz())
            if band not in pref:
                return f"VarAC moved to {band or 'an unknown band'} while the dialog was open"
            return None
        return check

    def _verify_tx_frequency(self, bc: Dict[str, Any]) -> None:
        """VarAC logs 'Sending Async message' when it really keys up, often seconds after the
        dialog closed (it waits for a clear channel).  Fix the logged frequency from that."""
        v = self._verify
        if not v:
            return
        try:
            hz = self.vc.tx_frequency_for(v["message"])
        except Exception:  # noqa: BLE001
            hz = None
        if hz is None:
            if time.time() > v["deadline"]:
                self._verify = None
            return
        self._verify = None
        if hz != v.get("frequency_hz"):
            try:
                self.repo.beacon_tx_set_frequency(v["row_id"], hz)
            except Exception as e:  # noqa: BLE001
                log.debug("could not update TX frequency: %s", e)
            with self._lock:
                lt = self.state.get("last_tx")
                if isinstance(lt, dict) and lt.get("requested_at") == v.get("requested_at"):
                    lt["frequency_hz"] = hz
        pref = self._preferred_bands(bc)
        band = freq_to_band(hz)
        if pref and band not in pref:
            log.warning("broadcast went out on %s (%s Hz), not a preferred band: VarAC's scanner moved while the "
                        "broadcast sat in its queue", band or "?", hz)
        else:
            log.info("VarAC transmitted on %s Hz (%s)", hz, band or "?")

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
            hold = self._band_hold(bc)
            self.state.update({"enabled": bool(bc.get("enabled")), "dry_run": bool(bc.get("dry_run")),
                               "mode": bc.get("mode"), "method": bc.get("method"), "limits": dict(LIMITS),
                               "effective": {"fixed": bc["fixed"], "smart": bc["smart"], "max_per_hour": bc["max_per_hour"]},
                               "last_tick": iso_utc(now_utc()), "current_band": self._current_band(),
                               "preferred_bands": self._preferred_bands(bc), "holding_for_band": None,
                               "pending": self._pending_summary(),
                               "scanner_dwell_seconds": self._scanner_dwell(),
                               "band_window_seconds": self._band_window(self._scanner_dwell()) if self._scanner_dwell() else None,
                               "band_age_seconds": (time.time() - self._freq_changed_at) if self._freq_changed_at else None,
                               "tx_last_hour": self.repo.beacon_tx_count_since(
                                   iso_utc(now_utc() - timedelta(hours=1)), real_only=False)})
            self._verify_tx_frequency(bc)
            self._service_pending(bc, fix, hold)
            self.state["pending"] = self._pending_summary()
            if not bc.get("enabled"):
                self.state.update({"blocked": None, "decision": "disabled", "next_due_seconds": None})
                self._block_logged = None
                return
            blocked = self._blocked_reason(bc, fix)
            self._note_block(blocked)
            if blocked:
                self.state.update({"blocked": blocked, "decision": "blocked", "next_due_seconds": None})
                return
            policy = self._policy_for(bc)
            pfix = Fix(fix.lat, fix.lon, fix.time, fix.speed_kmh, fix.course_deg)
            d = policy.evaluate(pfix, now_utc())
            self.state.update({"blocked": None, "decision": d.reason, "next_due_seconds": d.next_due_seconds})
            if d.send:
                if hold:
                    # Due, but VarAC's scanner is on another band: keep it pending.  The policy
                    # keeps saying "send" each tick until the band comes round.
                    self.state.update({"decision": "holding_band", "holding_for_band": hold})
                    return
                self._transmit(bc, fix, d.reason)

    # -- manual sends waiting for a preferred band ---------------------------------------
    def _pending_summary(self) -> Optional[Dict[str, Any]]:
        p = self._pending
        if not p:
            return None
        return {"kind": p["kind"], "message": p["message"], "requested_at": p["requested_at"],
                "expires_at": p["expires_at"], "waiting_for": p["waiting_for"]}

    def _queue_pending(self, bc: Dict[str, Any], kind: str, trigger: str, message: Optional[str], hold: str) -> Dict[str, Any]:
        now = now_utc()
        wait = max(60, int(bc.get("band_wait_max_seconds") or 600))
        self._pending = {"kind": kind, "trigger": trigger, "message": message, "requested_at": iso_utc(now),
                         "expires_at": iso_utc(now + timedelta(seconds=wait)), "waiting_for": hold}
        self.state["pending"] = self._pending_summary()
        log.info("manual send queued: %s (%s)", kind, hold)
        return {"ok": True, "queued": True, "message": message, "waiting_for": hold, "expires_in_seconds": wait}

    def _service_pending(self, bc: Dict[str, Any], fix: Optional[OwnFix], hold: Optional[str]) -> None:
        p = self._pending
        if not p:
            return
        now = now_utc()
        if parse_iso(p["expires_at"]) and now > parse_iso(p["expires_at"]):
            log.warning("queued manual send expired without VarAC reaching a preferred band")
            self.repo.beacon_tx_add(requested_at=p["requested_at"], sent_at=None, lat=None, lon=None, grid=None,
                                    trigger=p["trigger"], method="broadcast", message=p["message"],
                                    dry_run=1 if bc.get("dry_run") else 0, ok=0,
                                    error="gave up waiting for a preferred band")
            self._pending = None
            return
        if hold:
            self._pending["waiting_for"] = hold
            return
        blocked = self._blocked_reason(bc, fix, manual=True)
        if blocked:
            return  # keep waiting; spacing / VarAC busy will clear
        self._pending = None
        self._transmit(bc, fix, p["trigger"], message=p["message"])

    def cancel_pending(self) -> bool:
        with self._lock:
            had = self._pending is not None
            self._pending = None
            self.state["pending"] = None
            return had

    # -- transmit ------------------------------------------------------------------
    def _transmit(self, bc: Dict[str, Any], fix: OwnFix, trigger: str, message: Optional[str] = None) -> Dict[str, Any]:
        method = (bc.get("method") or "broadcast").lower()
        dry = bool(bc.get("dry_run"))
        if message is None:
            message = self.build_message(fix, bc) if method == "broadcast" else f"(one-time advanced beacon: grid {fix.grid})"
        else:
            method = "broadcast"   # relayed text always goes out as a broadcast
        requested = iso_utc(now_utc())
        try:   # frequency when we handed the message over; verified against VarAC.log afterwards
            freq = self._observe_frequency()
        except Exception:
            freq = None
        ok, err = True, None
        if not dry:
            if method == "beacon":
                ok, err = varac_tx.send_one_time_beacon()
            else:
                ok, err = varac_tx.send_broadcast(message, bc.get("broadcast_to") or "ALL",
                                                  precheck=self._band_precheck(bc))
        aborted = (not ok) and str(err or "").startswith("aborted")
        row = dict(requested_at=requested, sent_at=iso_utc(now_utc()) if ok else None, lat=fix.lat, lon=fix.lon,
                   grid=fix.grid, trigger=trigger, method=method, message=message, dry_run=1 if dry else 0,
                   ok=1 if ok else 0, error=err or None, frequency_hz=freq)
        row_id = None
        try:
            row_id = self.repo.beacon_tx_add(**row)
        except Exception as e:
            log.warning("could not log beacon_tx: %s", e)
        if ok:
            policy = self._policy_for(bc)
            policy.mark_sent(Fix(fix.lat, fix.lon, fix.time, fix.speed_kmh, fix.course_deg), now_utc())
            self._last_fail_at = 0.0
            self._fail_streak = 0
            if not dry and method == "broadcast" and row_id is not None:
                self._verify = {"row_id": row_id, "message": message, "frequency_hz": freq,
                                "requested_at": requested, "deadline": time.time() + VERIFY_TX_SECONDS}
            log.info("%s %s via %s: %s", "DRY-RUN" if dry else "SENT", trigger, method, message)
            if not dry and not trigger.startswith("relay"):
                try:   # stage 3: mirror our own position to APRS through Graywolf (no-op unless enabled)
                    gtx = getattr(self.rt, "graywolf_tx", None)
                    if gtx is not None:
                        gtx.mirror(fix, trigger)
                except Exception as e:  # noqa: BLE001
                    log.debug("APRS mirror hook failed: %s", e)
        elif aborted:
            # Not a failure: we chose not to send (band moved).  No backoff; try the next window.
            self._activity_at = 0.0
            log.info("send %s: %s", trigger, err)
        else:
            self._fail_streak += 1
            self._last_fail_at = time.time()
            self._last_fail_reason = err or "unknown"
            self._activity_at = 0.0   # re-read VarAC's state on the next tick
            log.warning("transmit failed (%s via %s): %s; next retry in %d s", trigger, method, err,
                        int(self._fail_backoff()))
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
            blocked = self._blocked_reason(bc, fix, manual=True)
            if blocked:
                return {"ok": False, "error": blocked}
            hold = self._band_hold(bc)
            if hold:
                return self._queue_pending(bc, "position", "manual", None, hold)
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
            blocked = self._blocked_reason(bc, fix, manual=True)
            if blocked:
                return {"ok": False, "error": blocked}
            msg = self.relay_message(station, comment)
            hold = self._band_hold(bc)
            if hold:
                return self._queue_pending(bc, "relay", f"relay:{station['callsign']}", msg, hold)
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
