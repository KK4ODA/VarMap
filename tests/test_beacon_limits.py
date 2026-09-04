"""Anti-spam limits that configuration cannot relax, and the calling-frequency rule."""
from varmap.services.beacon import LIMITS, clamp_beacon_config


def test_intervals_are_floored():
    bc = clamp_beacon_config({"fixed": {"interval_seconds": 30, "max_interval_seconds": 60, "min_move_m": 5},
                              "smart": {"min_interval_seconds": 10, "fast_rate_seconds": 20, "slow_rate_seconds": 60,
                                        "max_interval_seconds": 90, "min_move_m": 1, "min_turn_time_seconds": 5},
                              "max_per_hour": 500})
    assert bc["fixed"]["interval_seconds"] == LIMITS["min_interval_seconds"]
    assert "max_interval_seconds" not in bc["fixed"] and "max_interval_seconds" not in bc["smart"]   # keepalive gone
    assert bc["fixed"]["only_if_moved"] is True and bc["smart"]["only_if_moved"] is True          # default on
    assert bc["fixed"]["min_move_m"] == LIMITS["min_move_m"]
    assert bc["smart"]["min_interval_seconds"] == LIMITS["min_interval_seconds"]
    assert bc["smart"]["fast_rate_seconds"] == LIMITS["min_interval_seconds"]
    assert bc["smart"]["slow_rate_seconds"] == LIMITS["min_stationary_seconds"]
    assert bc["smart"]["min_turn_time_seconds"] == LIMITS["min_interval_seconds"]
    assert bc["max_per_hour"] == LIMITS["max_per_hour"] == 6
    assert bc["max_per_day"] == LIMITS["max_per_day"]


def test_sane_values_untouched():
    bc = clamp_beacon_config({"fixed": {"interval_seconds": 900, "min_move_m": 500},
                              "smart": {"min_interval_seconds": 300, "fast_rate_seconds": 300, "slow_rate_seconds": 1800,
                                        "min_move_m": 500, "min_turn_time_seconds": 300},
                              "max_per_hour": 2})
    assert bc["fixed"]["interval_seconds"] == 900 and bc["smart"]["fast_rate_seconds"] == 300 and bc["max_per_hour"] == 2


def test_defaults_are_within_limits():
    from varmap.config import DEFAULT_CONFIG
    d = DEFAULT_CONFIG["beacon"]
    assert d["fixed"]["only_if_moved"] is True and d["smart"]["only_if_moved"] is True
    assert d["max_per_hour"] == 2 <= LIMITS["max_per_hour"]
    c = clamp_beacon_config(d)
    assert c["fixed"]["interval_seconds"] == d["fixed"]["interval_seconds"]      # HF defaults need no clamping
    assert c["smart"]["min_interval_seconds"] == d["smart"]["min_interval_seconds"] == 600


def _install(d, ini_text, conf_text):
    from varmap.config import Config
    from varmap.integration.varac_config import VaracConfig
    (d / "VarAC.exe").write_bytes(b"x")
    (d / "VarAC.ini").write_text(ini_text, encoding="utf-8")
    (d / "VarAC_frequencies.conf").write_text(conf_text, encoding="utf-8")
    cfg = Config(str(d / "config.json"))
    cfg.update({"data_dir": str(d), "varac": {"exe_path": str(d / "VarAC.exe")}}, save=False)
    return VaracConfig(cfg)


# First block = calling frequencies (whatever it is called); later blocks = groups / EmComm nets.
CONF = "****MY DEFAULTS****\n14.105.000|20m\n7.105.000|40m\n7.333.000|extra CF\n****EDM****\n7.119.750|net\n****NFARES****\n3.589.250|net\n"


def test_calling_frequency_detection(tmp_path):
    vc = _install(tmp_path, "[MY_INFO]\nMycall=KK4ODA\n[QSO]\nConsiderAllFreqListAsCF=OFF\n", CONF)
    cfs = vc.calling_frequencies_hz()
    assert 7333000 in cfs and 7105000 in cfs and 14105000 in cfs       # first block, whatever its name
    assert 7119750 not in cfs and 3589250 not in cfs                    # group / net blocks are not CFs
    assert 3595000 in cfs                                               # VarAC's built-in list always included
    (tmp_path / "VarAC.log").write_text("x - CAT: Changing frequency to 7106500\n", encoding="utf-8")
    assert vc.on_calling_frequency() == 7105000                         # a slot 1.5 kHz up still counts
    (tmp_path / "VarAC.log").write_text("x - CAT: Changing frequency to 7119750\n", encoding="utf-8")
    assert vc.on_calling_frequency() is None                            # net frequency: smart timing allowed


def test_all_list_as_cf(tmp_path):
    vc = _install(tmp_path, "[QSO]\nConsiderAllFreqListAsCF=ON\n", CONF)
    (tmp_path / "VarAC.log").write_text("x - CAT: Changing frequency to 7119750\n", encoding="utf-8")
    assert vc.on_calling_frequency() == 7119750                         # VarAC treats the whole list as CF


def test_missing_conf_falls_back_to_builtin(tmp_path):
    from varmap.config import Config
    from varmap.integration.varac_config import VaracConfig
    (tmp_path / "VarAC.exe").write_bytes(b"x")
    (tmp_path / "VarAC.ini").write_text("[MY_INFO]\nMycall=KK4ODA\n", encoding="utf-8")
    cfg = Config(str(tmp_path / "config.json"))
    cfg.update({"data_dir": str(tmp_path), "varac": {"exe_path": str(tmp_path / "VarAC.exe")}}, save=False)
    vc = VaracConfig(cfg)
    assert 7105000 in vc.calling_frequencies_hz() and 14105000 in vc.calling_frequencies_hz()
