"""A relayed position ('APRS KN4PLO <GPS:..> via KK4ODA') belongs to KN4PLO, never to KK4ODA."""
import os
import sqlite3
import tempfile

from varmap.domain.gpstag import parse_relay
from varmap.integration.varac_db import VaracDbSource
from varmap.storage.repository import Repository
from tests.test_ingest import FakeRuntime, make_varac_db, ts


def test_parse_relay():
    assert parse_relay("APRS KN4PLO <GPS:33.56950,-85.07483> via KK4ODA") == ("KN4PLO", "KK4ODA")
    assert parse_relay("«APRS KN4PLO-9 «GPS:33.5,-85.0» via KK4ODA»".strip("«»")) == ("KN4PLO-9", "KK4ODA")
    assert parse_relay("<GPS:33.86,-84.30> EM73UU VarMap APRS:Y") is None
    assert parse_relay("KK4ODA checking in via HF") is None


def test_relay_broadcast_is_attributed_to_the_named_station():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "VarAC.db")
    make_varac_db(db, [(ts(30), 2, "KK4ODA", None, "EM73UU", 1, "")],
                  broadcasts=[(ts(10), "KK4ODA", "APRS KN4PLO <GPS:33.56950,-85.07483> via KK4ODA", None)])
    rt = FakeRuntime(tmp, db)
    rt.cfg.update({"own_station": {"callsign": "KK4ODA"}}, save=False)
    from varmap.services.poller import Poller
    Poller(rt).poll_once()
    me = rt.repo.station("KK4ODA")
    assert me["grid"] == "EM73UU" and me["position_source"] == "beacon"      # my own grid, not KN4PLO's fix
    assert me["heard_count"] == 2                                            # the broadcast still counts as heard
    them = rt.repo.station("KN4PLO")
    assert them["position_source"] == "relayed" and abs(them["lat"] - 33.5695) < 1e-6
    assert them["last_text"].startswith("relayed on VarAC by KK4ODA")


def test_repair_of_old_databases():
    """Databases written by <= 0.3.0 carry the sender-attributed rows; init_schema repairs them."""
    from datetime import datetime, timezone
    from varmap.integration.contracts import Observation
    d = tempfile.mkdtemp()
    repo = Repository(os.path.join(d, "t.db"))
    now = datetime(2026, 9, 3, 19, 0, tzinfo=timezone.utc)
    beacon = Observation(callsign="KK4ODA", heard_at=now.replace(hour=18), source="beacon", source_ref="x|cq:1",
                         frame_kind="beacon", lat=33.85417, lon=-84.29167, grid="EM73UU", accuracy_m=3800)
    wrong = Observation(callsign="KK4ODA", heard_at=now, source="broadcast_gps", source_ref="x|broadcast:9",
                        frame_kind="broadcast", lat=33.5695, lon=-85.07483, accuracy_m=10,
                        text="APRS KN4PLO <GPS:33.56950,-85.07483> via KK4ODA")
    repo.ingest([beacon, wrong], {}, now=now)
    assert repo.station("KK4ODA")["position_source"] == "broadcast_gps"      # the old, wrong state
    repo.meta_set("repair_relay_attribution", "0")
    fixed = repo._repair_relay_attribution()
    assert fixed == 1
    st = repo.station("KK4ODA")
    assert st["position_source"] == "beacon" and st["grid"] == "EM73UU" and st["position_count"] == 1
    assert repo._repair_relay_attribution() == 0                              # runs once


def test_damaged_database_is_quarantined_and_rebuilt():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "varmap.db")
    with open(p, "wb") as f:
        f.write(b"SQLite format 3\x00" + b"\xff" * 4000)        # header ok, pages garbage
    repo = Repository(p, check_integrity=True)
    assert repo.integrity.startswith("damaged") and repo.quarantined and os.path.isfile(repo.quarantined)
    assert repo.counts()["stations"] == 0 and repo.counts()["db_integrity"].startswith("damaged")
    repo.close()
    repo2 = Repository(p, check_integrity=True)                    # the rebuilt file is healthy
    assert repo2.integrity == "ok" and repo2.quarantined is None


def test_integrity_check_is_skipped_while_another_process_holds_the_db():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "varmap.db")
    Repository(p).close()                                          # create a healthy DB (WAL mode)
    holder = sqlite3.connect(p, isolation_level=None)
    holder.execute("PRAGMA journal_mode = WAL")
    holder.execute("BEGIN IMMEDIATE")                              # simulate the running VarMap mid-write
    holder.execute("INSERT INTO app_meta(key, value) VALUES('x','y')")
    repo = Repository(p, check_integrity=True)
    assert repo.integrity.startswith("unchecked (database in use") and repo.quarantined is None
    assert os.path.isfile(p)                                       # never renamed away from the holder
    holder.execute("ROLLBACK"); holder.close()


def test_integrity_check_runs_when_db_is_free():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "varmap.db")
    Repository(p).close()
    repo = Repository(p, check_integrity=True)
    assert repo.integrity == "ok" and repo.quarantined is None
