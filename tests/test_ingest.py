"""Offline integration test: a tiny synthetic VarAC database -> poller -> repository."""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

from varmap.config import Config
from varmap.integration.varac_db import VaracDbSource, validate_database
from varmap.storage.repository import Repository


def make_varac_db(path, rows, broadcasts=(), first_login="2025-10-29 22:20:36"):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE parameter(parameter_id INTEGER PRIMARY KEY, parameter_name TEXT, parameter_value TEXT);
    CREATE TABLE cqframe_type(cqframe_type_id INTEGER PRIMARY KEY, cqframe_type TEXT);
    INSERT INTO cqframe_type VALUES(1,'CQ'),(2,'BEACON');
    CREATE TABLE cqframe(id INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT NOT NULL, cqframe_time DATETIME,
      cqframe_type_id INTEGER, frequency INTEGER, bandwidth TEXT, from_callsign TEXT, snr INTEGER, slot TEXT,
      data TEXT, locator TEXT, is_emcomm BOOLEAN NOT NULL DEFAULT 0, band TEXT, is_email_gateway BOOLEAN NOT NULL DEFAULT 0,
      instance_id INTEGER, is_ai_gateway BOOLEAN NOT NULL DEFAULT 0, is_bbs BOOLEAN NOT NULL DEFAULT 0, diploma TEXT);
    CREATE TABLE broadcast(id INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT NOT NULL, broadcast_time DATETIME,
      frequency INTEGER, from_callsign TEXT, to_callsign TEXT NOT NULL, via_callsign TEXT NOT NULL,
      broadcast_message TEXT NOT NULL, snr INTEGER, band TEXT, instance_id INTEGER);
    """)
    c.execute("INSERT INTO parameter(parameter_name, parameter_value) VALUES('db_version','36')")
    c.execute("INSERT INTO parameter(parameter_name, parameter_value) VALUES('first_login_time',?)", (first_login,))
    for r in rows:
        c.execute("INSERT INTO cqframe(guid, cqframe_time, cqframe_type_id, frequency, from_callsign, snr, locator, band, is_bbs, data)"
                  " VALUES('g',?,?,7090250,?,?,?,'40m',?,?)", r)
    for b in broadcasts:
        c.execute("INSERT INTO broadcast(guid, broadcast_time, frequency, from_callsign, to_callsign, via_callsign, broadcast_message, snr, band)"
                  " VALUES('g',?,7090250,?,'ALL','',?,?,'40m')", b)
    c.commit()
    c.close()


def ts(minutes_ago, base=None):
    base = base or datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    return (base - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S.0000000Z")


class FakeRuntime:
    def __init__(self, tmp, db):
        self.cfg = Config(os.path.join(tmp, "config.json"))
        self.cfg.update({"data_dir": tmp, "varac": {"db_path": db}}, save=False)
        from varmap.integration.varac_config import VaracConfig
        self.vc = VaracConfig(self.cfg)
        self.repo = Repository(self.cfg.db_path())
        self.new_data = 0

    def on_new_data(self, n):
        self.new_data += n


def test_end_to_end_poll_and_replay():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "VarAC.db")
    rows = [
        (ts(50), 2, "KQ4WUH", 9, "FM05JL", 1, ""),
        (ts(40), 2, "K5OOM", -4, "", 0, ""),               # standard beacon: no locator
        (ts(30), 2, "KQ4WUH", 7, "FM05JL ⌛", 1, ""),      # away
        (ts(20), 1, "W0ALX", 2, "EN34AB", 0, "POTA"),
        (ts(10), 2, "KK4ODA", None, "EM73UU", 1, ""),      # own
        (ts(5), 2, "KQ4WUH", 8, "CN87OD", 1, ""),          # implausible jump -> suspect
    ]
    bcs = [(ts(15), "KQ4SAX", "«NAME:Franco» «QTH:Duluth» «LOC:EM73WX»", 3),
           (ts(14), "N9PMI", "Paul in Louisville, KY checking in  73", 2),
           (ts(13), "AA5GW", "<GPS:35.42864, -99.40437> mobile", 5)]
    make_varac_db(db, rows, bcs)
    assert validate_database(db)["ok"]

    rt = FakeRuntime(tmp, db)
    rt.cfg.update({"own_station": {"callsign": "KK4ODA"}}, save=False)
    from varmap.services.poller import Poller
    p = Poller(rt)
    p.poll_once()
    s = p.snapshot()
    assert s["connected"] and s["status"] == "ok", s
    assert s["last_batch"] == 9

    st = rt.repo.station("KQ4WUH")
    assert st["heard_count"] == 3 and st["position_count"] == 3
    assert st["grid"] == "CN87OD" and st["position_suspect"] == 1     # newest wins, flagged suspect
    assert st["is_away"] == 0                                          # newest frame was not away
    k5 = rt.repo.station("K5OOM")
    assert k5["lat"] is None and k5["heard_count"] == 1
    assert rt.repo.station("W0ALX")["last_cq_tag"] == "POTA"
    assert rt.repo.station("KK4ODA")["is_own"] == 1
    assert rt.repo.station("KQ4SAX")["grid"] == "EM73WX" and rt.repo.station("KQ4SAX")["position_source"] == "broadcast_grid"
    assert rt.repo.station("AA5GW")["position_source"] == "broadcast_gps"
    assert rt.repo.station("N9PMI")["lat"] is None and rt.repo.station("N9PMI")["last_text"].startswith("Paul")

    # Replay: nothing changes
    before = rt.repo.counts()
    p.poll_once()
    assert rt.repo.counts()["frames"] == before["frames"]
    assert p.snapshot()["last_batch"] == 0

    # New frame arrives
    c = sqlite3.connect(db)
    c.execute("INSERT INTO cqframe(guid, cqframe_time, cqframe_type_id, frequency, from_callsign, snr, locator, band)"
              " VALUES('g',?,2,7090250,'K5OOM',-2,'EM12TQ','40m')", (ts(1),))
    c.commit()
    c.close()
    p.poll_once()
    assert rt.repo.station("K5OOM")["grid"] == "EM12TQ"
    assert p.snapshot()["last_batch"] == 1

    # Database replaced (restore): cursor namespace changes, no collision
    os.remove(db)
    make_varac_db(db, rows[:2], first_login="2026-01-01 00:00:00")
    p.poll_once()
    assert p.snapshot()["status"] == "ok"
    assert rt.repo.station("KQ4WUH")["heard_count"] == 4   # re-ingested under the new identity


def test_backfill_policies():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "VarAC.db")
    rows = [(ts(60 * 24 * 40), 2, "OLD1", 1, "EM73UU", 0, ""), (ts(60), 2, "NEW1", 1, "EM73UU", 0, "")]
    make_varac_db(db, rows)
    from varmap.services.poller import Poller
    rt = FakeRuntime(tmp, db)
    rt.cfg.update({"varac": {"backfill": "days:30"}}, save=False)
    p = Poller(rt)
    p.poll_once()
    assert rt.repo.station("NEW1") and rt.repo.station("OLD1") is None

    tmp2 = tempfile.mkdtemp()
    rt2 = FakeRuntime(tmp2, db)
    rt2.cfg.update({"varac": {"backfill": "none"}}, save=False)
    Poller(rt2).poll_once()
    assert rt2.repo.counts()["stations"] == 0


def test_source_identity_in_ref():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "VarAC.db")
    make_varac_db(db, [(ts(1), 2, "KQ4WUH", 9, "FM05JL", 1, "")])
    src = VaracDbSource(db, "KK4ODA")
    conn = src.open()
    src.probe(conn)
    obs = src.cqframe_to_observation(src.fetch_cqframes(conn, 0, 10)[0])
    assert obs.source_ref.startswith(src.identity + "|cqframe:")
    conn.close()
