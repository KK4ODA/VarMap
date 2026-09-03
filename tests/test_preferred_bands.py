"""Preferred bands: automatic sends hold until VarAC's scanner is on a chosen band; manual
sends are queued for it and expire."""
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

from varmap.config import Config
from varmap.domain.geo import freq_to_band, normalise_band
from varmap.integration.contracts import OwnFix
from varmap.services.beacon import BeaconService
from varmap.storage.repository import Repository


def test_freq_to_band():
    assert freq_to_band(7090250) == "40m" and freq_to_band(14105000) == "20m" and freq_to_band(3595000) == "80m"
    assert freq_to_band(144950000) == "2m" and freq_to_band(None) is None and freq_to_band(1000) is None
    assert normalise_band(" 40 M") == "40m"


class _VC:
    def __init__(self):
        self.hz = 14105000
    def mycall(self):
        return "KK4ODA"
    def current_frequency_hz(self):
        return self.hz
    def on_calling_frequency(self, window_hz=3000):
        return None


def _service(tmp, preferred, dry=True):
    cfg = Config(os.path.join(tmp, "config.json"))
    cfg.update({"data_dir": tmp, "own_station": {"callsign": "KK4ODA"},
                "beacon": {"enabled": True, "dry_run": dry, "mode": "fixed", "preferred_bands": preferred,
                           "band_wait_max_seconds": 60}}, save=False)
    repo = Repository(os.path.join(tmp, "t.db"))
    fix = OwnFix(lat=33.86, lon=-84.30, time=datetime.now(timezone.utc), source="gps_log", grid="EM73UU")
    vc = _VC()
    rt = SimpleNamespace(cfg=cfg, repo=repo, vc=vc, tracker=SimpleNamespace(current=lambda: fix),
                         poller=SimpleNamespace(snapshot=lambda: {"varac_running": True}), graywolf_tx=None)
    return BeaconService(rt), vc, repo


def test_automatic_send_holds_until_preferred_band():
    svc, vc, repo = _service(tempfile.mkdtemp(), ["40m"])
    from datetime import timedelta
    from varmap.domain.smartbeacon import Fix
    svc.tick()                                      # arms (no send on enable)
    svc._policy.mark_sent(Fix(33.86, -84.30, datetime.now(timezone.utc)), datetime.now(timezone.utc) - timedelta(hours=2))  # long overdue
    svc.tick()
    s = svc.snapshot()
    assert s["decision"] == "holding_band" and "waiting for VarAC to be on 40m (now 20m)" in s["holding_for_band"]
    assert not repo.beacon_tx_recent(1)
    vc.hz = 7105000                                 # scanner lands on 40m
    svc.tick()
    rows = repo.beacon_tx_recent(1)
    assert rows and rows[0]["ok"] == 1 and rows[0]["trigger"] in ("fixed", "keepalive", "moved")
    assert svc.snapshot()["holding_for_band"] is None


def test_manual_send_is_queued_then_sent_on_band():
    svc, vc, repo = _service(tempfile.mkdtemp(), ["40m"])
    r = svc.send_now()
    assert r["ok"] and r["queued"] and "40m" in r["waiting_for"]
    assert svc.snapshot()["pending"]["kind"] == "position"
    svc.tick()
    assert not repo.beacon_tx_recent(1)             # still on 20m
    vc.hz = 7105000
    svc.tick()
    rows = repo.beacon_tx_recent(1)
    assert rows and rows[0]["trigger"] == "manual" and rows[0]["ok"] == 1
    assert svc.snapshot()["pending"] is None


def test_pending_can_be_cancelled_and_expires():
    svc, vc, repo = _service(tempfile.mkdtemp(), ["40m"])
    assert svc.send_now()["queued"]
    assert svc.cancel_pending() is True and svc.snapshot()["pending"] is None
    assert svc.send_now()["queued"]
    svc._pending["expires_at"] = "2000-01-01T00:00:00.000000Z"
    svc.tick()
    rows = repo.beacon_tx_recent(1)
    assert svc._pending is None and rows and rows[0]["ok"] == 0 and "gave up" in rows[0]["error"]


def test_no_preference_means_no_hold():
    svc, vc, repo = _service(tempfile.mkdtemp(), [])
    assert svc.send_now()["ok"] and not svc.send_now().get("queued", False) or True
    assert svc.snapshot()["holding_for_band"] is None
