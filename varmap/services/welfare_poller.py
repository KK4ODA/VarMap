"""Watches the Emcomm BBS welfare CSV and answers "has this callsign checked in?".

Own thread, like the Graywolf poller: Emcomm BBS being absent, stopped or in
the middle of rewriting the file must never touch VarAC ingest or the web
server.  A missing folder or file is the normal "no board" state; a parse
failure keeps the previous board and is reported in the snapshot.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional

from ..domain.timeparse import iso_utc, now_utc
from ..integration.welfare_board import WelfareBoard, WelfareBoardError, board_dir, latest_board_csv, parse_board_csv

log = logging.getLogger("varmap.welfare")


class WelfarePoller(threading.Thread):
    def __init__(self, rt) -> None:
        super().__init__(name="varmap-welfare", daemon=True)
        self.rt = rt
        self.cfg = rt.cfg
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._board = WelfareBoard()
        self._sig: Optional[tuple] = None      # (path, mtime, size) of the parsed file
        self.state: Dict[str, Any] = {
            "enabled": False, "dir": None, "file": None, "found": False, "count": 0, "by_status": {},
            "board_date": None, "window": None, "file_mtime": None, "age_s": None, "stale": False,
            "last_poll": None, "last_error": None,
        }

    # -- control -------------------------------------------------------------
    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.state)

    def lookup(self, callsign: str) -> Optional[Dict[str, Any]]:
        """Welfare entry for a station, or None.  Cheap and thread-safe."""
        with self._lock:
            if not self.state["enabled"] or not self.state["found"]:
                return None
            board = self._board
        entry = board.lookup(callsign)
        if entry:
            entry["stale"] = bool(self.state.get("stale"))
        return entry

    def rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            board = self._board
        return board.rows()

    # -- loop ----------------------------------------------------------------
    def run(self) -> None:
        log.info("welfare poller started")
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # pragma: no cover - last-resort guard
                log.exception("welfare poll failed unexpectedly: %s", e)
                with self._lock:
                    self.state["last_error"] = f"{type(e).__name__}: {e}"
            iv = max(5, int(self.cfg.get("welfare", "poll_interval_seconds") or 15))
            self._wake.wait(iv)
            self._wake.clear()
        log.info("welfare poller stopped")

    def poll_once(self) -> None:
        w = self.cfg.get("welfare") or {}
        now = now_utc()
        if not w.get("enabled", True):
            with self._lock:
                self._board = WelfareBoard()
                self._sig = None
                self.state.update({"enabled": False, "found": False, "count": 0, "by_status": {},
                                   "last_poll": iso_utc(now)})
            return

        directory = board_dir(self.cfg, getattr(self.rt, "vc", None))
        path = latest_board_csv(directory)
        if not path:
            with self._lock:
                had = self.state["found"]
                self._board = WelfareBoard()
                self._sig = None
                self.state.update({"enabled": True, "dir": directory, "file": None, "found": False, "count": 0,
                                   "by_status": {}, "board_date": None, "window": None, "file_mtime": None,
                                   "age_s": None, "stale": False, "last_poll": iso_utc(now), "last_error": None})
            if had:
                log.info("welfare board gone from %s", directory)
            return

        try:
            st = os.stat(path)
            sig = (path, st.st_mtime, st.st_size)
        except OSError as e:
            with self._lock:
                self.state.update({"enabled": True, "dir": directory, "last_poll": iso_utc(now), "last_error": str(e)})
            return

        max_age = float(w.get("max_age_hours") or 24) * 3600
        age = max(0.0, now.timestamp() - st.st_mtime)
        error = None
        if sig != self._sig:
            try:
                board = WelfareBoard(parse_board_csv(path))
                with self._lock:
                    self._board = board
                    self._sig = sig
                log.info("welfare board %s: %d check-in(s) %s", os.path.basename(path), board.count, board.by_status)
            except WelfareBoardError as e:
                error = str(e)     # keep the previous board; the file is probably being rewritten
                log.debug("welfare: %s", e)

        with self._lock:
            board = self._board
            self.state.update({
                "enabled": True, "dir": directory, "file": os.path.basename(path), "found": True,
                "count": board.count, "by_status": dict(board.by_status), "board_date": board.date or None,
                "window": board.window or None, "file_mtime": iso_utc(now.__class__.fromtimestamp(st.st_mtime, tz=now.tzinfo)),
                "age_s": int(age), "stale": age > max_age, "last_poll": iso_utc(now), "last_error": error,
            })
