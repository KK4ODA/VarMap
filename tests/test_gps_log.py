"""VarAC_gps.log parsing.  The 'Lat:/Long:' lines are the real VarAC V15.0.18
format, captured live on 2026-09-02."""
import os
import tempfile
from datetime import datetime, timezone

import pytest

from varmap.integration.varac_gps import parse_gps_log_line, parse_nmea_sentence, parse_varac_gps_line, read_gps_log


def test_varac_line_without_fix_is_none():
    assert parse_varac_gps_line("2026-09-02 22:09:10,Lat: °  Long: ° ") is None
    assert parse_gps_log_line("2026-09-02 22:09:10,Lat: °  Long: ° \r") is None


def test_varac_line_with_fix():
    f = parse_gps_log_line("2026-09-02 22:09:10,Lat: 33.86000°  Long: -84.30000° \r")
    assert f["lat"] == pytest.approx(33.86000) and f["lon"] == pytest.approx(-84.30000)
    assert f["time"] == datetime(2026, 9, 2, 22, 9, 10, tzinfo=timezone.utc)
    assert f["speed_kmh"] is None and f["course_deg"] is None


def test_varac_live_line_with_fix():
    # Captured from C:\VarAC\VarAC_gps.log on 2026-09-02 once the GPS had a fix
    f = parse_gps_log_line("2026-09-02 22:11:55,Lat: 33°50.3528 N Long: 084°16.5223 W\r")
    assert f["lat"] == pytest.approx(33 + 50.3528 / 60, abs=1e-6)
    assert f["lon"] == pytest.approx(-(84 + 16.5223 / 60), abs=1e-6)
    assert f["time"] == datetime(2026, 9, 2, 22, 11, 55, tzinfo=timezone.utc)


def test_varac_line_variants():
    assert parse_gps_log_line("2026-09-02 22:09:10,Lat: 33.86000 Long: -84.30000")["lat"] == pytest.approx(33.86000)
    f = parse_gps_log_line("2026-09-02 22:09:10,Lat: 33° 50.338' N  Long: 84° 16.527' W")
    assert f["lat"] == pytest.approx(33 + 50.338 / 60) and f["lon"] == pytest.approx(-(84 + 16.527 / 60))
    assert parse_gps_log_line("2026-09-02 22:09:10,Lat: 0°  Long: 0°") is None


def test_other_formats_still_work():
    assert parse_gps_log_line("33.86000,-84.30000")["lon"] == pytest.approx(-84.30000)
    assert parse_gps_log_line("2026-09-02 01:00:00,33.86000,-84.30000,42,180")["speed_kmh"] == 42
    assert parse_nmea_sentence("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230326,003.1,W*6A")["time"].year == 2026


def test_read_gps_log_takes_last_fix_and_skips_empty_tail():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "VarAC_gps.log")
    with open(p, "w", newline="", encoding="utf-8") as fh:   # VarAC writes UTF-8 (C2 B0 for the degree sign)
        fh.write("2026-09-02 22:09:08,Lat: 33.85900°  Long: -84.29900° \r\n")
        fh.write("2026-09-02 22:09:09,Lat: 33.86000°  Long: -84.30000° \r\n")
        for i in range(10, 20):
            fh.write(f"2026-09-02 22:09:{i},Lat: °  Long: ° \r\n")
    f = read_gps_log(p)
    assert f["lat"] == pytest.approx(33.86000)
    assert f["time"] == datetime(2026, 9, 2, 22, 9, 9, tzinfo=timezone.utc)   # the fix's own time, not the file mtime


def test_read_gps_log_all_empty_is_none():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "VarAC_gps.log")
    with open(p, "w", newline="", encoding="utf-8") as fh:   # VarAC writes UTF-8 (C2 B0 for the degree sign)
        for i in range(10, 20):
            fh.write(f"2026-09-02 22:09:{i},Lat: °  Long: ° \r\n")
    assert read_gps_log(p) is None
