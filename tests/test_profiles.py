"""Issue #1: several VarAC profiles (cluster / one instance per radio)."""
import os
import sqlite3
import tempfile

from tests.test_ingest import make_varac_db, ts
from varmap.config import Config
from varmap.integration.varac_config import VaracConfig
from varmap.storage.repository import Repository


def _ini(path, call, db=""):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"[MY_INFO]\nMycall={call}\n[OTHER]\nDBCustomFilePath={db}\n[VARAC_CLUSTER]\nClusterEnabled=ON\n")


def _install():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "VarAC.exe"), "wb").write(b"x")
    _ini(os.path.join(d, "VarAC.ini"), "KK4ODA")
    _ini(os.path.join(d, "VarAC_991A_SSB.ini"), "KQ4QWH")                       # shares VarAC.db
    other = os.path.join(d, "fm")
    os.makedirs(other)
    _ini(os.path.join(d, "VarAC_Anytone.ini"), "KQ4QWH", other)                  # its own database
    for helper in ("VarAC_UI_languages.ini", "VarAC_cat_commands.ini", "VarAC_templates.ini"):
        with open(os.path.join(d, helper), "w", encoding="utf-8") as f:
            f.write("[English-template]\nx=y\n")
    cfg = Config(os.path.join(d, "config.json"))
    cfg.update({"data_dir": d, "varac": {"exe_path": os.path.join(d, "VarAC.exe")}}, save=False)
    return d, other, cfg


def test_list_profiles_skips_helper_ini_files():
    d, other, cfg = _install()
    names = [p["name"] for p in VaracConfig(cfg).list_profiles()]
    assert names == ["VarAC.ini", "VarAC_991A_SSB.ini", "VarAC_Anytone.ini"]
    by = {p["name"]: p for p in VaracConfig(cfg).list_profiles()}
    assert by["VarAC.ini"]["is_main"] and by["VarAC_991A_SSB.ini"]["same_db_as_main"]
    assert not by["VarAC_Anytone.ini"]["same_db_as_main"]
    assert by["VarAC_Anytone.ini"]["db_path"] == os.path.join(other, "VarAC.db")


def test_sources_dedupe_shared_database():
    d, other, cfg = _install()
    cfg.update({"varac": {"extra_profiles": ["VarAC_991A_SSB.ini", "VarAC_Anytone.ini", "Missing.ini"]}}, save=False)
    src = VaracConfig(cfg).sources()
    assert src[0]["main"] and src[0]["also"] == ["VarAC_991A_SSB.ini"]          # shared DB read once
    assert src[1]["profile"] == "VarAC_Anytone.ini" and src[1]["db_path"] == os.path.join(other, "VarAC.db")
    assert src[2]["profile"] == "Missing.ini" and "not found" in src[2]["error"]
    assert len(src) == 3


def test_poller_reads_every_profile_database():
    d, other, cfg = _install()
    make_varac_db(os.path.join(d, "VarAC.db"), [(ts(10), 2, "W1HF", 5, "FN42AA", 0, "")])
    make_varac_db(os.path.join(other, "VarAC.db"), [(ts(5), 2, "W2FM", 7, "FN30AA", 0, "")],
                  first_login="2026-03-03 03:03:03")
    cfg.update({"varac": {"extra_profiles": ["VarAC_Anytone.ini"]}}, save=False)

    class RT:
        pass
    rt = RT()
    rt.cfg, rt.vc = cfg, VaracConfig(cfg)
    rt.repo = Repository(cfg.db_path())
    rt.new = 0
    rt.on_new_data = lambda n: setattr(rt, "new", rt.new + n)
    from varmap.services.poller import Poller
    p = Poller(rt)
    p.poll_once()
    assert rt.repo.station("W1HF") and rt.repo.station("W2FM")
    srcs = p.snapshot()["sources"]
    assert [s["status"] for s in srcs] == ["ok", "ok"] and rt.new == 2
    p.poll_once()                                                               # replay: nothing new
    assert rt.new == 2

    # A new frame in the extra database only
    c = sqlite3.connect(os.path.join(other, "VarAC.db"))
    c.execute("INSERT INTO cqframe(guid, cqframe_time, cqframe_type_id, frequency, from_callsign, snr, locator, band)"
              " VALUES('g',?,2,145050000,'W3FM',1,'FM19AA','2m')", (ts(1),))
    c.commit()
    c.close()
    p.poll_once()
    assert rt.repo.station("W3FM") and rt.new == 3

    # An extra database going missing does not break the main one
    os.remove(os.path.join(other, "VarAC.db"))
    p.poll_once()
    s = p.snapshot()
    assert s["status"] == "ok" and s["sources"][1]["status"] == "error"
