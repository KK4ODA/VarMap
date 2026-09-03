"""Graywolf (APRS) integration: DTO mapping and the cookie-session client against a fake server."""
import json
import os
import tempfile
from datetime import datetime, timezone

import pytest

from varmap.integration.graywolf import (GraywolfClient, GraywolfError, parse_rfc3339, position_to_own_fix,
                                         station_to_observation)

STATION = {
    "callsign": "KK4ODA-9", "channel": 1, "comment": "VarMap test /A=001050", "direction": "RX", "gated": False,
    "hops": 1, "is_object": False, "last_heard": "2026-09-03T14:05:07.123456789Z", "path": "WIDE1-1,WIDE2-1",
    "positions": [{"lat": 33.86, "lon": -84.30, "timestamp": "2026-09-03T14:05:07Z"},
                  {"lat": 33.85, "lon": -84.31, "timestamp": "2026-09-03T13:55:00Z"}],
    "symbol_code": ">", "symbol_table": "/", "via": "W4DOC-3",
}


def test_rfc3339_variants():
    assert parse_rfc3339("2026-09-03T14:05:07.123456789Z") == datetime(2026, 9, 3, 14, 5, 7, 123456, tzinfo=timezone.utc)
    assert parse_rfc3339("2026-09-03T10:05:07-04:00") == datetime(2026, 9, 3, 14, 5, 7, tzinfo=timezone.utc)
    assert parse_rfc3339("") is None and parse_rfc3339("nope") is None


def test_station_mapping():
    o = station_to_observation(STATION)
    assert o.callsign == "KK4ODA-9" and o.source == "aprs" and o.frame_kind == "aprs"
    assert o.lat == 33.86 and o.lon == -84.30 and o.grid == "EM73UU"
    assert o.symbol == "/>" and o.band == "APRS" and o.text.startswith("VarMap test")
    assert o.heard_at == datetime(2026, 9, 3, 14, 5, 7, 123456, tzinfo=timezone.utc)
    assert o.source_ref.startswith("gw|aprs:KK4ODA-9:")
    assert station_to_observation({**STATION, "last_heard": "2026-09-03T14:06:00Z"}).source_ref != o.source_ref


def test_station_without_position_and_object():
    o = station_to_observation({"callsign": "N0CALL", "last_heard": "2026-09-03T14:05:07Z", "positions": []})
    assert o is not None and not o.has_position
    obj = station_to_observation({**STATION, "callsign": "REPEATER", "is_object": True, "source": "KK4ODA"})
    assert obj.is_object and obj.raw["originator"] == "KK4ODA"
    assert station_to_observation({"callsign": "", "positions": []}) is None
    nul = station_to_observation({**STATION, "positions": [{"lat": 0, "lon": 0}]})
    assert not nul.has_position                                   # null island rejected


def test_position_to_own_fix():
    f = position_to_own_fix({"valid": True, "source": "gps", "lat": 33.86, "lon": -84.30, "speed_kt": 10.0,
                             "has_course": True, "heading_deg": 90.0, "has_alt": True, "alt_m": 300.0,
                             "timestamp": "2026-09-03T14:05:07Z"})
    assert f.source == "graywolf" and f.fix_quality == "gps"
    assert f.speed_kmh == pytest.approx(18.52) and f.course_deg == 90.0 and f.altitude_m == 300.0
    assert f.time == datetime(2026, 9, 3, 14, 5, 7, tzinfo=timezone.utc)
    fixed = position_to_own_fix({"valid": True, "source": "fixed", "lat": 33.86, "lon": -84.30, "speed_kt": 0})
    assert fixed.fix_quality == "fixed" and fixed.speed_kmh is None
    assert position_to_own_fix({"valid": False}) is None
    assert position_to_own_fix({"valid": True, "source": "none", "lat": 1, "lon": 1}) is None


class _FakeServer:
    """Minimal stand-in for Graywolf: cookie login, 401 without it, stations with ETag."""

    def __init__(self):
        self.logins = 0
        self.calls = []

    def handle(self, method, path, params, body, headers, cookie):
        self.calls.append((method, path, dict(params or {}), dict(headers or {})))
        if path == "/version":
            return 200, {"version": "1.2.3"}, {}
        if path == "/auth/login":
            self.logins += 1
            if body.get("password") == "secret":
                return 200, {"status": "ok"}, {"set-cookie": "session=abc"}
            return 401, {"error": "bad credentials"}, {}
        if cookie != "abc":
            return 401, {"error": "authentication required"}, {}
        if path == "/stations":
            if (headers or {}).get("If-None-Match") == '"v1"':
                return 304, None, {"etag": '"v1"'}
            return 200, [STATION], {"etag": '"v1"'}
        if path == "/position":
            return 200, {"valid": True, "source": "gps", "lat": 33.86, "lon": -84.30, "speed_kt": 0}, {}
        return 404, {"error": "nope"}, {}


@pytest.fixture
def client(monkeypatch):
    srv = _FakeServer()
    c = GraywolfClient("http://gw.test:8080", "op", "secret")
    state = {"cookie": None}

    def fake_raw(method, path, params=None, body=None, headers=None):
        status, payload, hdrs = srv.handle(method, path, params, body or {}, headers, state["cookie"])
        if "set-cookie" in hdrs:
            state["cookie"] = hdrs["set-cookie"].split("=", 1)[1]
        return status, payload, hdrs

    monkeypatch.setattr(c, "_raw", fake_raw)
    return c, srv


def test_client_logs_in_on_demand_and_uses_etag(client):
    c, srv = client
    sts, etag = c.stations(timerange=3600)
    assert srv.logins == 1 and len(sts) == 1 and etag == '"v1"'
    sts2, etag2 = c.stations(timerange=3600, etag=etag)
    assert sts2 is None and etag2 == '"v1"'                     # 304 -> nothing new
    assert srv.logins == 1                                       # cookie reused
    assert c.position()["valid"] is True
    assert c.test()["ok"] and c.test()["version"] == "1.2.3"


def test_client_bad_password(client):
    c, srv = client
    c.password = "wrong"
    with pytest.raises(GraywolfError):
        c.stations()
    assert c.test()["ok"] is False


def test_ingest_aprs_station_into_repository():
    from varmap.storage.repository import Repository
    repo = Repository(os.path.join(tempfile.mkdtemp(), "t.db"))
    o = station_to_observation(STATION)
    stats = repo.ingest([o], {"graywolf@test": "x"})
    assert stats.heard_inserted == 1 and stats.positions_inserted == 1
    st = repo.station("KK4ODA-9")
    assert st["position_source"] == "aprs" and st["aprs_symbol"] == "/>" and st["last_band"] == "APRS"
    assert repo.ingest([o], {}).heard_inserted == 0              # replay is a no-op
