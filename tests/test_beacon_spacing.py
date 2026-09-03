"""Spacing guard: 5 minutes for the automatic scheduler, 60 s for manual buttons."""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from varmap.config import Config
from varmap.domain.timeparse import iso_utc
from varmap.integration.contracts import OwnFix
from varmap.services.beacon import LIMITS, BeaconService, clamp_beacon_config
from varmap.storage.repository import Repository


def _service(tmp, seconds_since_last_tx):
    cfg = Config(os.path.join(tmp, "config.json"))
    cfg.update({"data_dir": tmp, "beacon": {"dry_run": True, "max_per_hour": 6}, "own_station": {"callsign": "KK4ODA"}}, save=False)
    repo = Repository(os.path.join(tmp, "t.db"))
    now = datetime.now(timezone.utc)
    repo.beacon_tx_add(requested_at=iso_utc(now - timedelta(seconds=seconds_since_last_tx)), sent_at=None, lat=1, lon=1,
                       grid="EM73UU", trigger="fixed", method="broadcast", message="x", dry_run=1, ok=1, error=None)
    fix = OwnFix(lat=33.86, lon=-84.30, time=now, source="gps_log", grid="EM73UU")
    rt = SimpleNamespace(cfg=cfg, repo=repo, vc=SimpleNamespace(mycall=lambda: "KK4ODA"),
                         tracker=SimpleNamespace(current=lambda: fix), poller=SimpleNamespace(snapshot=lambda: {"varac_running": True}))
    svc = BeaconService(rt)
    return svc, clamp_beacon_config(cfg.get("beacon")), fix


def test_automatic_keeps_five_minutes():
    svc, bc, fix = _service(tempfile.mkdtemp(), 136)
    reason = svc._blocked_reason(bc, fix, manual=False)
    assert reason and "minimum spacing" in reason and "floor 300 s" in reason


def test_manual_only_needs_sixty_seconds():
    svc, bc, fix = _service(tempfile.mkdtemp(), 136)
    assert svc._blocked_reason(bc, fix, manual=True) is None
    svc2, bc2, fix2 = _service(tempfile.mkdtemp(), 20)
    reason = svc2._blocked_reason(bc2, fix2, manual=True)
    assert reason and "floor 60 s for manual sends" in reason
    assert LIMITS["manual_min_spacing_seconds"] == 60 < LIMITS["min_interval_seconds"]


def test_graywolf_rows_do_not_count_as_varac_spacing():
    svc, bc, fix = _service(tempfile.mkdtemp(), 900)
    svc.repo.beacon_tx_add(requested_at=iso_utc(datetime.now(timezone.utc)), sent_at=None, lat=1, lon=1, grid="",
                           trigger="gate:X", method="graywolf_object", message="obj", dry_run=1, ok=1, error=None)
    assert svc._blocked_reason(bc, fix, manual=False) is None          # an APRS object just sent is not a VarAC transmission
