"""APRS consent token, object naming/ambiguity, beacon bodies and the gating selection rule."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

from varmap.domain.gpstag import parse_aprs_consent
from varmap.integration.contracts import Observation, OwnFix
from varmap.integration.graywolf import grid_ambiguity, mirror_beacon_body, object_beacon_body, object_name_for
from varmap.storage.repository import Repository

NOW = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)


def test_consent_token():
    assert parse_aprs_consent("<GPS:33.86000,-84.30000> EM73UU VarMap APRS:Y") is True
    assert parse_aprs_consent("hello APRS:N") is False
    assert parse_aprs_consent("«GPS:33.86,-84.3» aprs:y") is True        # mangled / lower case
    assert parse_aprs_consent("APRS=Y") is True
    assert parse_aprs_consent("no token here") is None
    assert parse_aprs_consent("CAPRS:Y") is None                          # not a word on its own
    assert parse_aprs_consent("") is None


def test_object_names_and_ambiguity():
    assert object_name_for("KK4ODA-9") == "KK4ODA-9"
    assert object_name_for("F/KK4ODA/P") == "KK4ODA"                      # too long -> base callsign
    assert object_name_for("VERYLONGCALL1/P") is None                     # nothing fits in 9 chars
    assert grid_ambiguity(None) == 0 and grid_ambiguity(10) == 0
    assert grid_ambiguity(3800) == 2                                       # 6-char grid
    assert grid_ambiguity(90000) == 3                                      # 4-char grid


def test_beacon_bodies():
    fix = OwnFix(lat=33.860004, lon=-84.300007, time=NOW, source="graywolf", grid="EM73UU", altitude_m=300)
    b = mirror_beacon_body(fix, "KK4ODA", {"mirror_symbol": "/>", "send_path": "is_only"})
    assert b["type"] == "position" and b["latitude"] == 33.86 and b["longitude"] == -84.30001
    assert b["symbol_table"] == "/" and b["symbol"] == ">" and b["send_path"] == "is_only"
    assert "[VarMap]" in b["comment"] and b["alt_ft"] == 984 and b["use_gps"] is False
    st = {"callsign": "KQ4WUH", "lat": 35.47917, "lon": -79.20833, "grid": "FM05JL", "accuracy_m": 3800, "last_band": "40m"}
    o = object_beacon_body(st, "KQ4WUH", "KK4ODA", {})
    assert o["type"] == "object" and o["object_name"] == "KQ4WUH" and o["ambiguity"] == 2
    assert o["comment"].startswith("VarAC 40m FM05JL") and o["send_path"] == "is_only"
    assert len(o["comment"]) <= 43


def _obs(call, lat, lon, minutes_ago, kind="broadcast", text="", consent=None, own=False, source=None):
    t = NOW - timedelta(minutes=minutes_ago)
    return Observation(callsign=call, heard_at=t, source=source or ("broadcast_gps" if lat is not None else "broadcast"),
                       source_ref=f"t|{call}|{minutes_ago}", frame_kind=kind, lat=lat, lon=lon,
                       grid="EM73UU" if lat is not None else None, accuracy_m=10.0 if lat is not None else None,
                       text=text, aprs_consent=consent, is_own=own)


def test_gate_candidates_enforce_consent():
    repo = Repository(os.path.join(tempfile.mkdtemp(), "t.db"))
    repo.ingest([
        _obs("YES1", 33.86, -84.30, 30, text="<GPS:33.86,-84.30> APRS:Y", consent=True),
        _obs("NO1", 33.87, -84.30, 30, text="<GPS:33.87,-84.30> APRS:N", consent=False),
        _obs("SILENT", 33.88, -84.30, 30, text="<GPS:33.88,-84.30>"),
        _obs("ME", 33.86, -84.30, 30, text="<GPS:33.86,-84.30> APRS:Y", consent=True, own=True),
        _obs("WITHDRAWN", 33.89, -84.30, 60, text="APRS:Y", consent=True),
        _obs("WITHDRAWN", 33.89, -84.30, 20, text="APRS:N", consent=False),
        _obs("APRSONLY", 33.90, -84.30, 10, kind="aprs", source="aprs", consent=True),
    ], {}, now=NOW)
    names = {c["callsign"] for c in repo.gate_candidates(max_position_age_s=86400 * 30)}
    assert names == {"YES1"}, names
    # After it is gated, the same position is not offered again until a newer one arrives
    repo.gate_upsert("YES1", object_name="YES1", beacon_id=7, last_sent_at=(NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                     last_lat=33.86, last_lon=-84.30, sent_count=1)
    assert repo.gate_candidates(86400 * 30) == []
    repo.ingest([_obs("YES1", 33.95, -84.30, -5, text="<GPS:33.95,-84.30> APRS:Y", consent=True)], {}, now=NOW + timedelta(minutes=5))
    cands = repo.gate_candidates(86400 * 30)
    assert len(cands) == 1 and cands[0]["beacon_id"] == 7 and cands[0]["lat"] == 33.95


def test_consent_survives_later_messages_without_token():
    repo = Repository(os.path.join(tempfile.mkdtemp(), "t.db"))
    repo.ingest([_obs("K1", 33.86, -84.30, 60, text="APRS:Y", consent=True),
                 _obs("K1", 33.86, -84.30, 10, text="just chatting")], {}, now=NOW)
    st = repo.station("K1")
    assert st["aprs_consent"] == 1 and st["aprs_consent_at"].startswith("2026-09-03T17:00")
