"""VarAC .ini reading, including the transient lock VarAC holds while saving settings."""
import os
import tempfile
import time

from varmap.config import Config
from varmap.integration import varac_config
from varmap.integration.varac_config import VaracConfig, parse_dotted_frequency, read_varac_ini

INI = """#VarAC ini file

[MY_INFO]
Mycall=KK4ODA
MyLocator=EM73UU
[RIG_CONTROL]
LastFrequency=7.090.250
[QSO]
LocatorsDistanceUnit=MI
[OTHER]
DBCustomFilePath=
[GPS]
GPSEnabled=ON
WriteGPSDataToFile=ON
WriteGPSDataToFileName=C:\\VarAC\\VarAC_gps.log
ManualGPSData=33.86000,-84.30000
"""


def _setup():
    d = tempfile.mkdtemp()
    ini = os.path.join(d, "VarAC.ini")
    with open(ini, "w", encoding="cp1252") as f:
        f.write(INI)
    with open(os.path.join(d, "VarAC.exe"), "wb") as f:
        f.write(b"x")
    cfg = Config(os.path.join(d, "config.json"))
    cfg.update({"data_dir": d, "varac": {"exe_path": os.path.join(d, "VarAC.exe")}}, save=False)
    return d, ini, VaracConfig(cfg)


def test_reads_values_and_frequency():
    d, ini, vc = _setup()
    assert vc.mycall() == "KK4ODA"
    assert vc.my_locator() == "EM73UU"
    assert vc.distance_unit() == "MI"
    assert vc.last_frequency_hz() == 7090250
    assert vc.db_path() == os.path.join(d, "VarAC.db")
    assert vc.gps_settings()["write_to_file"] is True
    assert parse_dotted_frequency("14.105.000") == 14105000
    assert "manual_data" not in vc.describe()["gps"]          # never leaks coordinates


def test_unreadable_ini_keeps_last_good_copy(monkeypatch):
    d, ini, vc = _setup()
    assert vc.mycall() == "KK4ODA"
    # Simulate VarAC rewriting the file under an exclusive lock: mtime changes, read fails.
    os.utime(ini, (time.time() + 5, time.time() + 5))
    monkeypatch.setattr(varac_config, "read_varac_ini", lambda p: None)
    assert vc.mycall() == "KK4ODA"                            # cached copy survives
    assert vc.db_path() == os.path.join(d, "VarAC.db")
    monkeypatch.undo()
    assert vc.mycall() == "KK4ODA"                            # and re-reads fine afterwards


def test_read_varac_ini_permission_error_returns_none(monkeypatch):
    d, ini, vc = _setup()
    import builtins
    real_open = builtins.open

    def denied(*a, **k):
        if a and str(a[0]).endswith("VarAC.ini"):
            raise PermissionError(13, "Permission denied")
        return real_open(*a, **k)

    monkeypatch.setattr(builtins, "open", denied)
    assert read_varac_ini(ini) is None


def test_current_frequency_prefers_varac_log():
    d, ini, vc = _setup()
    assert vc.current_frequency_hz() == 7090250            # no log: falls back to the .ini
    with open(os.path.join(d, "VarAC.log"), "w", encoding="utf-8") as f:
        f.write("03/09/2026 00:33:30 - CAT: Changing frequency to 7105000\n")
        f.write("03/09/2026 00:33:30 - Scanner - Changing frequency: 14.105.000\n")
    assert vc.current_frequency_hz() == 14105000           # newest QSY line wins
