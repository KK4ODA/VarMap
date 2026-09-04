"""Guards added after the 2026-09-04 log review: parked stations stay silent, scanner-stop send
window, pre-click band re-check, session-lock hold, exponential retry backoff, and the
real TX frequency taken from VarAC.log."""
import os
import tempfile
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from varmap.config import Config
from varmap.domain.smartbeacon import Fix, FixedIntervalPolicy, SmartBeaconPolicy
from varmap.integration.contracts import OwnFix
from varmap.services import beacon as beacon_mod
from varmap.services.beacon import BeaconService, clamp_beacon_config
from varmap.storage.repository import Repository

T0 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def at(s):
    return T0 + timedelta(seconds=s)


def test_parked_station_is_silent_fixed():
    p = FixedIntervalPolicy({"interval_seconds": 600, "only_if_moved": True, "min_move_m": 500})
    f = Fix(33.85, -84.29, at(0))
    p.evaluate(f, at(0))                                  # arm
    for s in (600, 3600, 36000, 360000):
        d = p.evaluate(f, at(s))
        assert not d.send and d.reason == "not_moved" and d.next_due_seconds is None
    assert p.evaluate(Fix(33.86, -84.29, at(700)), at(700)).send   # moved: sends


def test_parked_station_is_silent_smart():
    p = SmartBeaconPolicy({"min_interval_seconds": 300, "slow_rate_seconds": 1800, "min_move_m": 500})
    f = Fix(33.85, -84.29, at(0), speed_kmh=0.0)
    p.evaluate(f, at(0))
    for s in (1800, 36000, 360000):
        assert not p.evaluate(f, at(s)).send


def test_clamp_drops_legacy_keepalive_and_defaults_only_if_moved():
    bc = clamp_beacon_config({"fixed": {"max_interval_seconds": 0}, "smart": {"max_interval_seconds": 0}})
    assert "max_interval_seconds" not in bc["fixed"] and "max_interval_seconds" not in bc["smart"]
    assert bc["fixed"]["only_if_moved"] is True and bc["smart"]["only_if_moved"] is True


class _VC:
    def __init__(self):
        self.hz = 7105000
        self.log_tail = ""

    def mycall(self):
        return "KK4ODA"

    def current_frequency_hz(self):
        return self.hz

    def on_calling_frequency(self, window_hz=3000):
        return None

    def tx_frequency_for(self, message):
        return 14105000 if message in self.log_tail else None


def _service(tmp, dry=True, preferred="40m"):
    cfg = Config(os.path.join(tmp, "config.json"))
    cfg.update({"data_dir": tmp, "own_station": {"callsign": "KK4ODA"},
                "beacon": {"enabled": True, "dry_run": dry, "mode": "fixed", "preferred_bands": preferred,
                           "band_wait_max_seconds": 60}}, save=False)
    repo = Repository(os.path.join(tmp, "t.db"))
    fix = OwnFix(lat=33.86, lon=-84.30, time=datetime.now(timezone.utc), source="gps_log", grid="EM73UU")
    vc = _VC()
    from types import SimpleNamespace
    rt = SimpleNamespace(cfg=cfg, repo=repo, vc=vc, tracker=SimpleNamespace(current=lambda: fix),
                         poller=SimpleNamespace(snapshot=lambda: {"varac_running": True}), graywolf_tx=None)
    return BeaconService(rt), vc, repo


def test_scanner_window_holds_late_in_stop():
    svc, vc, repo = _service(tempfile.mkdtemp())
    svc._observe_frequency()
    svc._dwells = deque([30.0, 31.0, 29.0], maxlen=8)
    svc._freq_changed_at = time.time() - 20            # 20 s into a ~30 s stop on 40m
    bc = clamp_beacon_config(svc.cfg.get("beacon"))
    hold = svc._band_hold(bc)
    assert hold and "waiting for the next 40m stop" in hold
    svc._freq_changed_at = time.time() - 1             # just landed on 40m
    assert svc._band_hold(bc) is None
    svc._dwells.clear()                                # scanner not active: no window restriction
    svc._freq_changed_at = time.time() - 500
    assert svc._band_hold(bc) is None


def _unlock_live_path(monkeypatch):
    monkeypatch.setattr(beacon_mod.varac_tx, "varac_activity", lambda: {"running": True, "busy": False})
    monkeypatch.setattr(beacon_mod.varac_tx, "ignore_dcd_state", lambda: False)
    monkeypatch.setattr(beacon_mod.varac_tx, "session_locked", lambda: False)


def test_precheck_aborts_when_band_moves_and_does_not_back_off(monkeypatch):
    svc, vc, repo = _service(tempfile.mkdtemp(), dry=False)
    _unlock_live_path(monkeypatch)
    seen = {}

    def fake_send(message, to="ALL", precheck=None):
        vc.hz = 14105000                               # scanner hops while the dialog is open
        seen["why"] = precheck() if precheck else None
        return (False, f"aborted before sending: {seen['why']}") if seen["why"] else (True, "")
    monkeypatch.setattr(beacon_mod.varac_tx, "send_broadcast", fake_send)
    r = svc.send_now()
    assert not r["ok"] and "aborted" in r["error"] and "20m" in seen["why"]
    rows = repo.beacon_tx_recent(1)
    assert rows[0]["ok"] == 0 and rows[0]["error"].startswith("aborted")
    assert svc._last_fail_at == 0.0 and svc._fail_streak == 0   # an abort is not a failure


def test_real_tx_frequency_is_verified_from_varac_log(monkeypatch):
    svc, vc, repo = _service(tempfile.mkdtemp(), dry=False)
    _unlock_live_path(monkeypatch)
    monkeypatch.setattr(beacon_mod.varac_tx, "send_broadcast", lambda m, to="ALL", precheck=None: (True, ""))
    r = svc.send_now()
    assert r["ok"] and svc._verify and svc._verify["frequency_hz"] == 7105000
    vc.log_tail = r["message"]                          # VarAC later logs the real TX ... on 20m
    svc.tick()
    assert svc._verify is None
    assert repo.beacon_tx_recent(1)[0]["frequency_hz"] == 14105000
    assert svc.snapshot()["last_tx"]["frequency_hz"] == 14105000


def test_session_lock_holds_sends(monkeypatch):
    svc, vc, repo = _service(tempfile.mkdtemp(), dry=False)
    _unlock_live_path(monkeypatch)
    monkeypatch.setattr(beacon_mod.varac_tx, "session_locked", lambda: True)
    r = svc.send_now()
    assert not r["ok"] and "locked" in r["error"]
    assert not repo.beacon_tx_recent(1)                 # nothing attempted, nothing logged as a failure


def test_failure_backoff_doubles_and_caps():
    svc, vc, repo = _service(tempfile.mkdtemp())
    for streak, expect in ((0, 120), (1, 120), (2, 240), (3, 480), (5, 1800), (9, 1800)):
        svc._fail_streak = streak
        assert svc._fail_backoff() == expect


def test_block_reason_logged_once_per_change(caplog):
    import logging
    svc, vc, repo = _service(tempfile.mkdtemp())
    with caplog.at_level(logging.INFO, logger="varmap.beacon"):
        svc._note_block("position fix is 901 s old (limit 900 s)")
        svc._note_block("position fix is 955 s old (limit 900 s)")   # same reason, other numbers
        svc._note_block(None)
    msgs = [r.getMessage() for r in caplog.records if "position TX" in r.getMessage()]
    assert len(msgs) == 2 and "held" in msgs[0] and "no longer" in msgs[1]
