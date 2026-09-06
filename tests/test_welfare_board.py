"""Emcomm BBS welfare board integration: CSV parsing, file selection, lookup, and the
poller's behaviour when the board is missing, half-written or stale."""
import os
import tempfile
import time

from varmap.config import Config
from varmap.integration.welfare_board import (WelfareBoard, board_dir, latest_board_csv, parse_board_csv)
from varmap.services.welfare_poller import WelfarePoller

# Exactly what Emcomm BBS 1.4+ writes (output_generator.generate_csv).
CSV = """Date,Window,Callsign,Name,Location,Status,Power,Contact,Message,Received_Time,Update_Number,Previous_Status
2026-09-06,00:00-23:59,W1ABC,Jane Doe,"Boston, MA",NEED ASSISTANCE,OFF,,"Power out since 12:00. Need generator fuel.
Also need insulin.",08:43:21,0,
2026-09-06,00:00-23:59,,Maria Garcia,"Decatur, GA",SAFE,OFF,404-555-0199,Family of 4 is safe.,08:43:40,0,
2026-09-06,00:00-23:59,KK4ODA,Facundo,"Atlanta, GA",TRAFFIC,ON,,Have traffic for net control,08:50:02,1,SAFE
"""


def _write(d, name, text, age_s=0):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if age_s:
        t = time.time() - age_s
        os.utime(p, (t, t))
    return p


class FakeVc:
    def __init__(self, bbs=""):
        self.bbs = bbs

    def value(self, section, key, default=""):
        return self.bbs if (section, key) == ("BBS", "BBSDirectory") else default


class FakeRt:
    def __init__(self, cfg, vc=None):
        self.cfg = cfg
        self.vc = vc or FakeVc()


def test_parse_emcomm_csv():
    d = tempfile.mkdtemp()
    p = _write(d, "welfare_2026-09-06_0000-2359.csv", CSV)
    b = WelfareBoard(parse_board_csv(p))
    assert b.count == 3 and b.by_status == {"NEED ASSISTANCE": 1, "SAFE": 1, "TRAFFIC": 1}
    assert b.date == "2026-09-06" and b.window == "00:00-23:59"
    w1 = b.lookup("w1abc")
    assert w1["status_key"] == "need" and w1["power"] == "OFF" and "insulin" in w1["message"]
    assert w1["received_at"] and w1["received_at"].endswith("Z")
    # name-only check-in is counted but cannot match a callsign
    assert b.lookup("MARIA") is None and "NAME:MARIA GARCIA" in b.entries
    # portable / mobile suffixes fall back to the base callsign
    assert b.lookup("KK4ODA/M")["status"] == "TRAFFIC"
    assert b.lookup("KK4ODA")["update_number"] == 1 and b.lookup("KK4ODA")["previous_status"] == "SAFE"
    assert b.lookup("") is None and b.lookup("N0CALL") is None


def test_half_written_file_does_not_raise():
    d = tempfile.mkdtemp()
    p = _write(d, "welfare_x.csv", CSV.split("\n")[0] + "\n2026-09-06,00:00-23:59,W1ABC,Jane")
    b = WelfareBoard(parse_board_csv(p))
    assert b.count == 0                       # no status column yet -> row ignored, no exception
    assert WelfareBoard(parse_board_csv(_write(d, "empty.csv", ""))).count == 0


def test_latest_file_and_dir_resolution():
    assert latest_board_csv(os.path.join(tempfile.gettempdir(), "does-not-exist-xyz")) is None
    d = tempfile.mkdtemp()
    assert latest_board_csv(d) is None
    old = _write(d, "welfare_2026-09-05_1900-2100.csv", CSV, age_s=3600)
    new = _write(d, "welfare_2026-09-06_0000-2359.csv", CSV)
    _write(d, "welfare_board.html", "<html>")
    assert latest_board_csv(d) == new and old != new

    cfg = Config(os.path.join(d, "config.json"))
    assert board_dir(cfg, FakeVc()) == r"C:\VarAC BBS"
    assert board_dir(cfg, FakeVc(r"D:\BBS")) == r"D:\BBS"
    cfg.update({"welfare": {"board_dir": d}}, save=False)
    assert board_dir(cfg, FakeVc(r"D:\BBS")) == d
    assert board_dir(cfg, None) == d


def test_poller_missing_then_present_then_stale():
    d = tempfile.mkdtemp()
    cfg = Config(os.path.join(d, "config.json"))
    board = os.path.join(d, "bbs")
    cfg.update({"data_dir": d, "welfare": {"board_dir": board, "max_age_hours": 1}}, save=False)
    p = WelfarePoller(FakeRt(cfg))

    p.poll_once()                               # folder does not exist: Emcomm BBS never ran
    s = p.snapshot()
    assert s["enabled"] and not s["found"] and s["count"] == 0 and s["last_error"] is None
    assert p.lookup("W1ABC") is None

    os.makedirs(board)
    _write(board, "welfare_2026-09-06_0000-2359.csv", CSV)
    p.poll_once()
    s = p.snapshot()
    assert s["found"] and s["count"] == 3 and s["file"].startswith("welfare_") and not s["stale"]
    assert p.lookup("W1ABC")["status"] == "NEED ASSISTANCE" and p.lookup("W1ABC")["stale"] is False
    assert [r["identifier"] for r in p.rows()][0] == "KK4ODA"   # newest first

    # a stale board (older than max_age_hours) is still served but flagged
    _write(board, "welfare_2026-09-06_0000-2359.csv", CSV, age_s=7200)
    p.poll_once()
    assert p.snapshot()["stale"] is True and p.lookup("W1ABC")["stale"] is True

    # unreadable rewrite keeps the last good board
    bad = os.path.join(board, "welfare_2026-09-06_0000-2359.csv")
    with open(bad, "wb") as f:
        f.write(b"\xff\xfe\x00garbage")
    p.poll_once()
    assert p.snapshot()["count"] == 3 and p.snapshot()["last_error"]

    # disabled: nothing is served
    cfg.update({"welfare": {"enabled": False}}, save=False)
    p.poll_once()
    assert p.snapshot()["enabled"] is False and p.lookup("W1ABC") is None

    # board removed again (Emcomm BBS output folder cleared)
    cfg.update({"welfare": {"enabled": True}}, save=False)
    os.remove(bad)
    p.poll_once()
    assert p.snapshot()["found"] is False and p.lookup("KK4ODA") is None
