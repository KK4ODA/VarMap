"""Update check and self-update.

Check:  GET https://api.github.com/repos/KK4ODA/VarMap/releases/latest (no login,
        60 requests/hour is plenty for one check every 12 h), compare the tag
        with our version, remember the answer.

Apply:  only for an INSTALLED Windows build (Inno Setup, detected by the
        uninstaller beside the exe).  Download VarMap-Setup-<ver>.exe to the
        data dir, verify size and SHA-256 against the release's SHA256SUMS.txt,
        then hand over to a tiny batch helper that waits for this process to
        exit, runs the installer silently and starts VarMap again.  Settings
        and the database live in %LOCALAPPDATA%\\VarMap, so they survive.
        Portable and source installs only get the download link.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .. import __version__
from ..domain.timeparse import iso_utc, now_utc

log = logging.getLogger("varmap.updater")

REPO = "KK4ODA/VarMap"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
USER_AGENT = f"VarMap/{__version__} (+https://github.com/{REPO})"


def parse_version(v: str) -> Tuple[int, ...]:
    """'v0.2.0' -> (0, 2, 0).  Non-numeric tails are ignored."""
    nums = re.findall(r"\d+", (v or "").split("-")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def install_mode() -> str:
    """'installed' (Inno Setup build), 'portable' (frozen, no uninstaller) or 'source'."""
    if not getattr(sys, "frozen", False):
        return "source"
    d = os.path.dirname(os.path.abspath(sys.executable))
    return "installed" if any(f.lower().startswith("unins") and f.lower().endswith(".exe") for f in os.listdir(d)) else "portable"


def pick_asset(assets: List[Dict[str, Any]], mode: str) -> Optional[Dict[str, Any]]:
    """The right download for this machine."""
    plat = sys.platform
    want: List[str] = []
    if plat == "win32":
        want = ["-Setup-", "windows-x64-portable"] if mode == "installed" else ["windows-x64-portable", "-Setup-"]
    elif plat == "darwin":
        import platform
        want = ["macos-arm64"] if platform.machine().lower() in ("arm64", "aarch64") else ["macos-x64"]
    else:
        want = ["linux-x64"]
    for key in want:
        for a in assets or []:
            if key in (a.get("name") or ""):
                return {"name": a["name"], "url": a.get("browser_download_url"), "size": a.get("size")}
    return None


def _http_json(url: str, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_text(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def helper_script(pid: int, installer: str, exe: str) -> str:
    """Batch helper: wait for VarMap to exit, install silently, relaunch VarMap."""
    return "\r\n".join([
        "@echo off",
        "title VarMap update",
        "echo Waiting for VarMap to close...",
        ":wait",
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto wait)',
        "echo Installing update...",
        f'"{installer}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /NOCANCEL',
        'if errorlevel 1 (echo Installer reported an error %errorlevel%. & pause & exit /b 1)',
        f'start "" "{exe}"',
        'del "%~f0" >nul 2>&1',
        "",
    ])


class Updater:
    def __init__(self, rt) -> None:
        self.rt = rt
        self.cfg = rt.cfg
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.state: Dict[str, Any] = {
            "current": __version__, "mode": install_mode(), "latest": None, "available": False,
            "notes": "", "url": RELEASES_PAGE, "asset": None, "checked_at": None, "error": None,
            "skipped": None, "applying": None, "can_self_update": install_mode() == "installed" and sys.platform == "win32",
        }

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="varmap-updater", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            s = dict(self.state)
        s["skipped"] = (self.cfg.get("updates", "skip_version") or "") or None
        s["available"] = bool(s.get("latest")) and is_newer(s["latest"], __version__) and (s["skipped"] != s["latest"])
        return s

    def _run(self) -> None:
        self._stop.wait(20)  # let the app come up first
        while not self._stop.is_set():
            u = self.cfg.get("updates") or {}
            if u.get("check", True):
                self.check()
            hours = max(1, float(u.get("interval_hours") or 12))
            self._stop.wait(hours * 3600)

    # -- check -------------------------------------------------------------------
    def check(self) -> Dict[str, Any]:
        try:
            rel = _http_json(API_LATEST)
            tag = rel.get("tag_name") or ""
            with self._lock:
                self.state.update({
                    "latest": tag.lstrip("v"), "notes": (rel.get("body") or "")[:4000],
                    "url": rel.get("html_url") or RELEASES_PAGE,
                    "asset": pick_asset(rel.get("assets") or [], self.state["mode"]),
                    "sums_url": next((a.get("browser_download_url") for a in rel.get("assets") or [] if a.get("name") == "SHA256SUMS.txt"), None),
                    "checked_at": iso_utc(now_utc()), "error": None,
                })
            if is_newer(tag, __version__):
                log.info("Update available: VarMap %s (running %s)", tag, __version__)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self.state.update({"checked_at": iso_utc(now_utc()), "error": str(e)})
            log.debug("update check failed: %s", e)
        return self.snapshot()

    def skip(self, version: Optional[str]) -> None:
        self.cfg.update({"updates": {"skip_version": version or ""}})

    # -- apply (Windows installed build only) ----------------------------------------
    def apply(self) -> Dict[str, Any]:
        s = self.snapshot()
        if not s["can_self_update"]:
            return {"ok": False, "error": "self-update is only available for the installed Windows build; use the download link"}
        if not s.get("available"):
            return {"ok": False, "error": "no update available"}
        asset = s.get("asset")
        if not asset or "-Setup-" not in asset["name"]:
            return {"ok": False, "error": "no installer asset in this release"}
        with self._lock:
            self.state["applying"] = "downloading"
        try:
            ddir = os.path.join(self.cfg.data_dir(), "updates")
            os.makedirs(ddir, exist_ok=True)
            path = os.path.join(ddir, asset["name"])
            req = urllib.request.Request(asset["url"], headers={"User-Agent": USER_AGENT})
            h = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
            size = os.path.getsize(path)
            if asset.get("size") and size != int(asset["size"]):
                raise RuntimeError(f"download size mismatch ({size} vs {asset['size']} bytes)")
            sums_url = s.get("sums_url")
            if sums_url:
                sums = _http_text(sums_url)
                expected = next((line.split()[0] for line in sums.splitlines() if line.strip().endswith(asset["name"])), None)
                if expected and expected.lower() != h.hexdigest().lower():
                    raise RuntimeError("SHA-256 checksum mismatch; download discarded")
                if expected:
                    log.info("update %s: checksum verified", asset["name"])
            with self._lock:
                self.state["applying"] = "installing"
            exe = sys.executable
            script = os.path.join(ddir, "apply_update.cmd")
            with open(script, "w", encoding="ascii", errors="replace") as f:
                f.write(helper_script(os.getpid(), path, exe))
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(["cmd.exe", "/c", "start", "VarMap update", "/min", script], creationflags=flags, close_fds=True)
            log.info("update helper launched; VarMap will exit in 2 s and be restarted by the installer helper")
            threading.Timer(2.0, lambda: os._exit(0)).start()
            return {"ok": True, "message": f"Installing VarMap {s['latest']}. VarMap will close and reopen in a moment."}
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self.state["applying"] = None
            log.warning("update failed: %s", e)
            return {"ok": False, "error": str(e)}
