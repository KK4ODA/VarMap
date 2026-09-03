from datetime import datetime, timedelta, timezone

from varmap.domain.callsign import normalise_callsign
from varmap.domain.geo import angle_diff_deg, bearing_deg, haversine_km
from varmap.domain.precedence import should_replace
from varmap.domain.staleness import classify
from varmap.domain.timeparse import iso_utc, parse_iso, parse_varac_time


def test_callsign():
    assert normalise_callsign("f/kk4oda/p") == ("F/KK4ODA/P", "KK4ODA")
    assert normalise_callsign(" kk4oda ") == ("KK4ODA", "KK4ODA")
    assert normalise_callsign("KK4ODA/M") == ("KK4ODA/M", "KK4ODA")
    assert normalise_callsign("") == ("", "")


def test_varac_time():
    t = parse_varac_time("2026-09-02 01:12:33.5707779Z")
    assert t == datetime(2026, 9, 2, 1, 12, 33, 570777, tzinfo=timezone.utc)
    assert parse_varac_time("2026-08-30 23:29:58") == datetime(2026, 8, 30, 23, 29, 58, tzinfo=timezone.utc)
    assert parse_varac_time("garbage") is None
    assert parse_varac_time(None) is None
    s = iso_utc(t)
    assert s == "2026-09-02T01:12:33.570777Z"
    assert parse_iso(s) == t


def test_iso_sorts_lexically():
    a = iso_utc(datetime(2026, 1, 1, tzinfo=timezone.utc))
    b = iso_utc(datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc))
    assert a < b


def test_precedence():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    def m(mins):
        return now - timedelta(minutes=mins)

    assert should_replace(None, None, "beacon", m(5), now)
    assert should_replace("beacon", m(10), "beacon", m(5), now)          # newer same rank wins
    assert not should_replace("beacon", m(5), "beacon", m(10), now)      # older same rank loses
    assert should_replace("beacon", m(20), "gps_tag", m(25), now)        # older, better, fresh wins
    assert not should_replace("beacon", m(20), "gps_tag", m(60 * 24 * 3), now)   # older, better, stale loses
    assert not should_replace("gps_tag", m(30), "broadcast_grid", m(5), now)     # newer but worse
    assert should_replace("gps_tag", m(60 * 10), "broadcast_grid", m(5), now)    # ...unless current is stale


def test_staleness():
    assert classify(10) == "fresh"
    assert classify(31 * 60) == "recent"
    assert classify(3 * 3600) == "stale"
    assert classify(2 * 86400) == "old"
    assert classify(40 * 86400) == "historic"
    assert classify(None) == "none"


def test_geo():
    # Atlanta EM73UU -> Sanford NC (KQ4WUH FM05JL); VarAC shows ~412 mi
    km = haversine_km(33.85417, -84.29167, 35.47917, -79.20833)
    assert 480 < km < 500
    b = bearing_deg(33.85417, -84.29167, 35.47917, -79.20833)
    assert 60 < b < 75
    assert angle_diff_deg(350, 10) == 20
    assert angle_diff_deg(90, 270) == 180
