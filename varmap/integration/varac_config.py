"""VarAC discovery and .ini reading.

Facts this relies on (design doc 2.1, 2.2):
* VarAC keeps VarAC.exe, its profile .ini files and (by default) VarAC.db in
  one flat directory.  The profile .ini name is VarAC's first command-line
  argument; blank means VarAC.ini.
* [OTHER] DBCustomFilePath relocates the database when non-empty.
* .ini files are not reliably UTF-8 (utf-8 -> cp1252 -> latin-1 fallback),
  sections are sometimes appended without blank lines, and values contain
  '%' and '$', so the parser must be strict=False, interpolation=None.
* LastFrequency uses dots as THOUSANDS separators: '7.090.250' = 7090250 Hz.
"""
from __future__ import annotations

import configparser
import os
import re
import threading
from typing import Any, Dict, List, Optional

VARAC_DEFAULT_DIR = r"C:\VarAC"
VARAC_DEFAULT_INI = "VarAC.ini"
VARAC_DB_NAME = "VarAC.db"
VARAC_EXE_NAME = "VarAC.exe"


def read_varac_ini(ini_path: str) -> Optional[configparser.ConfigParser]:
    """HamLink's proven encoding-fallback reader, hardened for VarAC quirks."""
    if not ini_path or not os.path.isfile(ini_path):
        return None
    for enc in ("utf-8", "cp1252", "latin-1"):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            with open(ini_path, "r", encoding=enc) as f:
                cp.read_file(f)
            return cp
        except (UnicodeDecodeError, UnicodeError):
            continue
        except configparser.Error:
            continue
        except OSError:
            # VarAC rewrites its .ini with an exclusive lock whenever settings are
            # saved; a PermissionError here is transient.  Caller keeps its cache.
            return None
    return None


def parse_dotted_frequency(raw: Optional[str]) -> Optional[int]:
    """'7.090.250  ' -> 7090250 Hz.  None if unparseable."""
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", str(raw))
    if not digits:
        return None
    return int(digits)


def _on(v: Optional[str]) -> bool:
    return (v or "").strip().upper() in ("ON", "TRUE", "1", "YES")


class VaracConfig:
    """Resolves where VarAC lives and exposes the .ini values we need.

    `cfg` is our own Config; the user's explicit paths always win over
    discovery.  Results are cached on the .ini's mtime.
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cp: Optional[configparser.ConfigParser] = None
        self._cp_mtime: Optional[float] = None
        self._cp_path: Optional[str] = None

    # -- discovery -------------------------------------------------------
    def exe_path(self) -> Optional[str]:
        p = (self.cfg.get("varac", "exe_path") or "").strip()
        if p and os.path.isfile(p):
            return p
        cand = os.path.join(VARAC_DEFAULT_DIR, VARAC_EXE_NAME)
        return cand if os.path.isfile(cand) else None

    def varac_dir(self) -> Optional[str]:
        exe = self.exe_path()
        if exe:
            return os.path.dirname(exe)
        db = (self.cfg.get("varac", "db_path") or "").strip()
        if db and os.path.isfile(db):
            return os.path.dirname(db)
        return VARAC_DEFAULT_DIR if os.path.isdir(VARAC_DEFAULT_DIR) else None

    def ini_path(self) -> Optional[str]:
        d = self.varac_dir()
        if not d:
            return None
        profile = (self.cfg.get("varac", "profile") or "").strip() or VARAC_DEFAULT_INI
        p = os.path.join(d, profile)
        return p if os.path.isfile(p) else None

    def db_path(self) -> Optional[str]:
        """Discovery ladder: explicit config > DBCustomFilePath > <dir>/VarAC.db > C:\\VarAC\\VarAC.db."""
        explicit = (self.cfg.get("varac", "db_path") or "").strip()
        if explicit:
            return explicit  # always honoured, even if missing (so the UI can say so)
        cp = self.ini()
        if cp is not None:
            custom = (cp.get("OTHER", "DBCustomFilePath", fallback="") or "").strip()
            if custom:
                if os.path.isdir(custom):
                    custom = os.path.join(custom, VARAC_DB_NAME)
                return custom
        d = self.varac_dir()
        if d:
            return os.path.join(d, VARAC_DB_NAME)
        return os.path.join(VARAC_DEFAULT_DIR, VARAC_DB_NAME)

    # -- .ini access -----------------------------------------------------
    def ini(self) -> Optional[configparser.ConfigParser]:
        path = self.ini_path()
        with self._lock:
            if not path:
                self._cp = None
                return None
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                self._cp = None
                return None
            if self._cp is not None and self._cp_path == path and self._cp_mtime == mtime:
                return self._cp
            cp = read_varac_ini(path)
            if cp is None:
                # Unreadable right now (VarAC saving settings): keep the last good
                # copy and retry on the next call instead of forgetting everything.
                return self._cp if self._cp_path == path else None
            self._cp, self._cp_path, self._cp_mtime = cp, path, mtime
            return self._cp

    def value(self, section: str, key: str, default: str = "") -> str:
        cp = self.ini()
        if cp is None:
            return default
        try:
            return (cp.get(section, key, fallback=default) or default).strip()
        except Exception:
            return default

    def mycall(self) -> str:
        own = (self.cfg.get("own_station", "callsign") or "").strip().upper()
        return own or self.value("MY_INFO", "Mycall").upper()

    def my_locator(self) -> str:
        return self.value("MY_INFO", "MyLocator").upper()

    def my_name(self) -> str:
        return self.value("MY_INFO", "MyName")

    def my_qth(self) -> str:
        return self.value("MY_INFO", "MyQTH")

    def distance_unit(self) -> str:
        u = self.value("QSO", "LocatorsDistanceUnit", "KM").upper()
        return "MI" if u.startswith("MI") else "KM"

    def last_frequency_hz(self) -> Optional[int]:
        return parse_dotted_frequency(self.value("RIG_CONTROL", "LastFrequency"))

    _FREQ_LOG_RE = re.compile(r"Changing frequency(?: to|:)\s*([0-9][0-9.]*)")

    def current_frequency_hz(self) -> Optional[int]:
        """The frequency VarAC is on right now.  VarAC.log records every QSY
        ('CAT: Changing frequency to 7090250', 'Scanner - Changing frequency:
        14.105.000'), which is more current than the .ini's LastFrequency."""
        d = self.varac_dir()
        log_path = os.path.join(d, "VarAC.log") if d else None
        if log_path and os.path.isfile(log_path):
            try:
                size = os.path.getsize(log_path)
                with open(log_path, "rb") as f:
                    f.seek(max(0, size - 65536))
                    tail = f.read().decode("utf-8", errors="replace")
                hits = self._FREQ_LOG_RE.findall(tail)
                if hits:
                    hz = parse_dotted_frequency(hits[-1])
                    if hz and hz > 100_000:
                        return hz
            except OSError:
                pass
        return self.last_frequency_hz()

    def tx_frequency_for(self, message: str, tail_bytes: int = 65536) -> Optional[int]:
        """The frequency VarAC was actually on when it transmitted `message`: VarAC.log's
        'Sending Async message: ... DATA:<message>' line, and the last 'Changing frequency'
        before it.  None until VarAC has logged the transmission (it queues broadcasts
        until the channel is clear, so this can lag the dialog by several seconds)."""
        d = self.varac_dir()
        p = os.path.join(d, "VarAC.log") if d else None
        msg = (message or "").strip()
        if not p or not msg or not os.path.isfile(p):
            return None
        try:
            size = os.path.getsize(p)
            with open(p, "rb") as f:
                f.seek(max(0, size - tail_bytes))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        idx = tail.rfind("Sending Async message")
        while idx != -1:
            end = tail.find("\n", idx)
            line = tail[idx:end if end != -1 else None]
            if msg in line:
                hits = self._FREQ_LOG_RE.findall(tail[:idx])
                hz = parse_dotted_frequency(hits[-1]) if hits else None
                return hz if hz and hz > 100_000 else None
            idx = tail.rfind("Sending Async message", 0, idx)
        return None

    def beacon_settings(self) -> Dict[str, Any]:
        return {
            "interval_minutes": int(self.value("BEACON", "BeaconIntervalMinutes", "15") or 15),
            "type": self.value("BEACON", "BeaconType", "").upper(),
            "grace_seconds": int(self.value("BEACON", "BeaconGraceTimeSeconds", "10") or 10),
        }

    def gps_settings(self) -> Dict[str, Any]:
        """The [GPS] section.  ManualGPSData is returned but must never be logged."""
        return {
            "enabled": _on(self.value("GPS", "GPSEnabled")),
            "com_port": self.value("GPS", "ComPort"),
            "baud": int(self.value("GPS", "BaudRate", "9600") or 9600),
            "write_to_file": _on(self.value("GPS", "WriteGPSDataToFile")),
            "file_path": self.value("GPS", "WriteGPSDataToFileName"),
            "manual_enabled": _on(self.value("GPS", "ManualGPSDataEnabled")),
            "manual_data": self.value("GPS", "ManualGPSData"),
            "verbose": _on(self.value("GPS", "VerboseGPS")),
        }

    def gps_log_path(self) -> Optional[str]:
        """Path of VarAC's GPS log if the user configured one for us, else the
        .ini path when WriteGPSDataToFile is ON, else the .ini path if the file
        simply exists (an operator may have enabled it earlier)."""
        override = (self.cfg.get("own_station", "gps_log_path") or "").strip()
        if override:
            return override
        g = self.gps_settings()
        p = g.get("file_path") or ""
        if p and (g.get("write_to_file") or os.path.isfile(p)):
            return p
        return None

    # VarAC's own standard calling frequencies (VarAC_frequencies.conf, 'STD VARAC FREQS'), used
    # as a fallback when the file cannot be read.
    STD_CALLING_FREQUENCIES_HZ = [14105000, 14109000, 7105000, 1995000, 3595000, 5355000, 10133000, 18107000,
                                  21105000, 24927000, 28105000, 50330000, 144170000, 144950000, 432550000, 439600000]

    def calling_frequencies_hz(self) -> List[int]:
        """Calling frequencies as VarAC sees them.

        VarAC_frequencies.conf is organised in '****' blocks: the FIRST block holds
        the calling frequencies (VarAC ships it as 'STD VARAC FREQS'); the blocks
        below are group, club and EmComm net frequencies and are NOT calling
        frequencies unless ConsiderAllFreqListAsCF=ON.  VarAC's built-in standard
        list is always included so a trimmed or renamed first block cannot hide one.
        """
        custom = self.value("RIG_CONTROL", "FrequenciesCustomFilePath") or self.value("OTHER", "FrequenciesCustomFilePath")
        d = self.varac_dir()
        path = custom if custom and os.path.isfile(custom) else (os.path.join(d, "VarAC_frequencies.conf") if d else None)
        all_sections = _on(self.value("QSO", "ConsiderAllFreqListAsCF"))
        out: List[int] = list(self.STD_CALLING_FREQUENCIES_HZ)
        if path and os.path.isfile(path):
            try:
                section_index = 0
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        s = line.strip()
                        if not s or s.startswith("#"):
                            continue
                        if s.startswith("****"):
                            section_index += 1
                            continue
                        if not all_sections and section_index > 1:
                            continue  # group / net blocks below the first one
                        hz = parse_dotted_frequency(s.split("|", 1)[0])
                        if hz and hz > 100_000:
                            out.append(hz)
            except OSError:
                pass
        return sorted(set(out))

    def on_calling_frequency(self, window_hz: int = 3000) -> Optional[int]:
        """The calling frequency VarAC is currently on (within window_hz, which
        covers VarAC's slots), or None."""
        cur = self.current_frequency_hz()
        if not cur:
            return None
        for cf in self.calling_frequencies_hz():
            if abs(cur - cf) <= window_hz:
                return cf
        return None

    def cluster_enabled(self) -> bool:
        return _on(self.value("VARAC_CLUSTER", "ClusterEnabled"))

    def describe(self) -> Dict[str, Any]:
        """Diagnostics.  Coordinates are deliberately redacted (privacy)."""
        g = self.gps_settings()
        return {
            "exe_path": self.exe_path(),
            "varac_dir": self.varac_dir(),
            "ini_path": self.ini_path(),
            "ini_readable": self.ini() is not None,
            "db_path": self.db_path(),
            "mycall": self.mycall(),
            "my_locator": self.my_locator(),
            "my_name": self.my_name(),
            "my_qth": self.my_qth(),
            "distance_unit": self.distance_unit(),
            "last_frequency_hz": self.current_frequency_hz(),
            "beacon": self.beacon_settings(),
            "gps": {k: v for k, v in g.items() if k != "manual_data"} | {"manual_data_present": bool(g.get("manual_data"))},
            "gps_log_path": self.gps_log_path(),
            "cluster_enabled": self.cluster_enabled(),
        }
