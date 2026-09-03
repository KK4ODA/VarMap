"""Polls Graywolf's station list and feeds APRS stations into the repository.
Separate thread from the VarAC poller: a Graywolf outage must not touch VarAC ingest."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from ..domain.timeparse import iso_utc, now_utc
from ..integration.graywolf import WORLD_BBOX, GraywolfClient, GraywolfError, station_to_observation

log = logging.getLogger("varmap.graywolf")
BACKOFF = [10, 30, 60, 300]


class GraywolfPoller(threading.Thread):
    def __init__(self, rt) -> None:
        super().__init__(name="varmap-graywolf", daemon=True)
        self.rt = rt
        self.cfg = rt.cfg
        self.repo = rt.repo
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._client: Optional[GraywolfClient] = None
        self._client_key = None
        self._etag: Optional[str] = None
        self._fail = -1
        self.state: Dict[str, Any] = {"enabled": False, "connected": False, "status": "off", "base": None,
                                      "version": None, "last_poll": None, "last_ok": None, "last_error": None,
                                      "stations_last": 0, "stations_total": 0, "backoff_seconds": 0}

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self.state.update(kw)

    def client(self) -> Optional[GraywolfClient]:
        """Shared client (also used by the own-position reader); rebuilt when settings change."""
        g = self.cfg.get("graywolf") or {}
        key = (g.get("url"), g.get("username"), g.get("password"))
        if self._client is None or key != self._client_key:
            self._client = GraywolfClient(g.get("url") or "http://127.0.0.1:8080", g.get("username") or "", g.get("password") or "")
            self._client_key = key
            self._etag = None
        return self._client

    def run(self) -> None:
        log.info("Graywolf poller started")
        while not self._stop.is_set():
            g = self.cfg.get("graywolf") or {}
            if g.get("enabled"):
                try:
                    self.poll_once()
                    self._fail = -1
                except GraywolfError as e:
                    self._failed(str(e))
                except Exception as e:  # noqa: BLE001
                    self._failed(f"{type(e).__name__}: {e}")
            else:
                self._set(enabled=False, connected=False, status="off")
            iv = max(3, int(g.get("poll_interval_seconds") or 10))
            wait = BACKOFF[self._fail] if self._fail >= 0 else iv
            self._set(backoff_seconds=wait if self._fail >= 0 else 0)
            self._wake.wait(wait)
            self._wake.clear()
        log.info("Graywolf poller stopped")

    def _failed(self, err: str) -> None:
        self._fail = min(self._fail + 1, len(BACKOFF) - 1)
        self._set(connected=False, status="error", last_error=err, last_poll=iso_utc(now_utc()))
        log.warning("Graywolf: %s", err)

    def poll_once(self) -> int:
        g = self.cfg.get("graywolf") or {}
        c = self.client()
        self._set(enabled=True, base=c.base)
        if c.version is None:
            try:
                c.get_version()
            except Exception:  # noqa: BLE001
                pass
        sid = f"graywolf@{c.base}"
        rec = self.repo.cursor_get(sid)
        since = rec["cursor"] if rec and rec.get("cursor") not in (None, "", "0") else None
        timerange = int(g.get("lookback_seconds") or 3600) if since else int(g.get("backfill_seconds") or 86400)
        bbox = (g.get("bbox") or "").strip() or WORLD_BBOX
        t0 = time.time()
        stations, etag = c.stations(bbox=bbox, timerange=timerange, since=since, etag=self._etag)
        self._etag = etag
        now_iso = iso_utc(now_utc())
        if stations is None:  # 304: nothing changed
            self._set(connected=True, status="ok", last_poll=now_iso, last_ok=now_iso, last_error=None,
                      version=c.version, stations_last=0, poll_ms=int((time.time() - t0) * 1000))
            return 0
        obs = []
        newest = None
        for st in stations:
            try:
                o = station_to_observation(st)
            except Exception as e:  # noqa: BLE001
                log.debug("Graywolf station unparseable: %s (%s)", st, e)
                o = None
            if o:
                obs.append(o)
                if newest is None or o.heard_at > newest:
                    newest = o.heard_at
        cursor = (newest.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z") if newest else (since or "")
        stats = self.repo.ingest(obs, {sid: cursor} if cursor else {})
        with self._lock:
            self.state.update({"connected": True, "status": "ok", "last_poll": now_iso, "last_ok": now_iso,
                               "last_error": None, "version": c.version, "stations_last": len(obs),
                               "stations_total": self.state["stations_total"] + stats.heard_inserted,
                               "poll_ms": int((time.time() - t0) * 1000)})
        if stats.heard_inserted:
            self.rt.on_new_data(stats.heard_inserted)
        return stats.heard_inserted
