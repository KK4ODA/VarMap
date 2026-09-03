"""Offline map tiles.

* TileStore     - an MBTiles file (SQLite, TMS row order) that caches every
                  tile the browser ever asked for, plus deliberately
                  downloaded regions.  Cache-first: the map keeps working with
                  no internet for any area you have visited or downloaded.
* TileFetcher   - polite upstream fetch (descriptive User-Agent, serialised,
                  small delay).  OpenStreetMap's tile usage policy discourages
                  bulk downloading; keep regions modest or point `source_url`
                  at a provider that allows it.
* RegionDownloader - background job over a bbox and zoom range with progress
                  and cancel.
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger("varmap.tiles")

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles (zoom_level, tile_column, tile_row);
CREATE TABLE IF NOT EXISTS regions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, south REAL, west REAL, north REAL, east REAL,
    zmin INTEGER, zmax INTEGER, tiles INTEGER, bytes INTEGER, created_at TEXT, source_url TEXT);
"""


def deg2num(lat: float, lon: float, z: int) -> Tuple[int, int]:
    n = 2 ** z
    lat = max(-85.05112878, min(85.05112878, lat))
    x = int((lon + 180.0) / 360.0 * n)
    lr = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lr) + 1.0 / math.cos(lr)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def region_tiles(south: float, west: float, north: float, east: float, zmin: int, zmax: int) -> Iterator[Tuple[int, int, int]]:
    for z in range(zmin, zmax + 1):
        x0, y0 = deg2num(north, west, z)
        x1, y1 = deg2num(south, east, z)
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                yield z, x, y


def count_region_tiles(south: float, west: float, north: float, east: float, zmin: int, zmax: int) -> int:
    total = 0
    for z in range(zmin, zmax + 1):
        x0, y0 = deg2num(north, west, z)
        x1, y1 = deg2num(south, east, z)
        total += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
    return total


class TileStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self._wlock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        c = self.conn()
        with self._wlock:
            c.executescript(SCHEMA)
            for k, v in (("name", "VarMap tiles"), ("format", "png"), ("type", "baselayer"), ("version", "1")):
                if not c.execute("SELECT 1 FROM metadata WHERE name=?", (k,)).fetchone():
                    c.execute("INSERT INTO metadata(name, value) VALUES(?, ?)", (k, v))

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15, check_same_thread=False, isolation_level=None)
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA synchronous = NORMAL")
            c.execute("PRAGMA busy_timeout = 15000")
            self._local.conn = c
        return c

    @staticmethod
    def _row(z: int, y: int) -> int:
        return (2 ** z - 1) - y  # XYZ -> TMS

    def get(self, z: int, x: int, y: int) -> Optional[bytes]:
        r = self.conn().execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                                (z, x, self._row(z, y))).fetchone()
        return bytes(r[0]) if r else None

    def has(self, z: int, x: int, y: int) -> bool:
        return self.conn().execute("SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                                   (z, x, self._row(z, y))).fetchone() is not None

    def put(self, z: int, x: int, y: int, data: bytes) -> None:
        with self._wlock:
            self.conn().execute("INSERT OR REPLACE INTO tiles(zoom_level, tile_column, tile_row, tile_data) VALUES(?,?,?,?)",
                                (z, x, self._row(z, y), sqlite3.Binary(data)))

    def stats(self) -> Dict[str, Any]:
        c = self.conn()
        n, b = c.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(tile_data)),0) FROM tiles").fetchone()
        by_zoom = {int(r[0]): int(r[1]) for r in c.execute("SELECT zoom_level, COUNT(*) FROM tiles GROUP BY 1 ORDER BY 1")}
        return {"tiles": n, "bytes": b, "by_zoom": by_zoom, "path": self.path,
                "file_bytes": os.path.getsize(self.path) if os.path.isfile(self.path) else 0}

    def regions(self) -> List[Dict[str, Any]]:
        return [dict(zip([d[0] for d in cur.description], r)) for cur in [self.conn().execute("SELECT * FROM regions ORDER BY id DESC")] for r in cur.fetchall()]

    def add_region(self, **row: Any) -> int:
        cols = ["name", "south", "west", "north", "east", "zmin", "zmax", "tiles", "bytes", "created_at", "source_url"]
        with self._wlock:
            cur = self.conn().execute(f"INSERT INTO regions({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                                      [row.get(k) for k in cols])
            return cur.lastrowid

    def delete_region(self, region_id: int, purge_tiles: bool = False) -> int:
        n = 0
        with self._wlock:
            c = self.conn()
            r = c.execute("SELECT * FROM regions WHERE id=?", (region_id,)).fetchone()
            if r and purge_tiles:
                cols = [d[0] for d in c.execute("SELECT * FROM regions LIMIT 0").description]
                reg = dict(zip(cols, r))
                for z, x, y in region_tiles(reg["south"], reg["west"], reg["north"], reg["east"], reg["zmin"], reg["zmax"]):
                    n += c.execute("DELETE FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                                   (z, x, self._row(z, y))).rowcount
            c.execute("DELETE FROM regions WHERE id=?", (region_id,))
        return n


class TileFetcher:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._last = 0.0
        self.last_error: Optional[str] = None
        self.fetched = 0
        self.failed = 0

    def url(self, z: int, x: int, y: int) -> str:
        tpl = self.cfg.get("tiles", "source_url") or "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        return tpl.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)).replace("{s}", "a")

    def fetch(self, z: int, x: int, y: int, timeout: float = 15.0, polite: bool = False) -> Optional[bytes]:
        """`polite=True` (bulk region downloads) serialises requests with a delay;
        interactive browser requests go straight through, like any web map."""
        ua = self.cfg.get("tiles", "user_agent") or "VarMap/0.1"
        if polite:
            delay = float(self.cfg.get("tiles", "download_delay_ms") or 50) / 1000.0
            with self._lock:
                wait = delay - (time.time() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()
        req = urllib.request.Request(self.url(z, x, y), headers={"User-Agent": ua, "Accept": "image/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if resp.status != 200 or not data or ("image" not in ctype and not data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1")):
                    self.failed += 1
                    self.last_error = f"unexpected response {resp.status} {ctype}"
                    return None
                self.fetched += 1
                self.last_error = None
                return data
        except Exception as e:
            self.failed += 1
            self.last_error = str(e)
            return None


class RegionDownloader:
    """One job at a time.  Progress is polled by the UI."""

    def __init__(self, store: TileStore, fetcher: TileFetcher, cfg) -> None:
        self.store, self.fetcher, self.cfg = store, fetcher, cfg
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.job: Optional[Dict[str, Any]] = None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.job) if self.job else {"active": False}

    def estimate(self, south: float, west: float, north: float, east: float, zmin: int, zmax: int) -> Dict[str, Any]:
        n = count_region_tiles(south, west, north, east, zmin, zmax)
        limit = int(self.cfg.get("tiles", "download_max_tiles") or 20000)
        return {"tiles": n, "limit": limit, "ok": n <= limit, "approx_mb": round(n * 25 / 1024.0, 1)}

    def start(self, name: str, south: float, west: float, north: float, east: float, zmin: int, zmax: int) -> Dict[str, Any]:
        with self._lock:
            if self.job and self.job.get("active"):
                return {"ok": False, "error": "a download is already running"}
        est = self.estimate(south, west, north, east, zmin, zmax)
        if not est["ok"]:
            return {"ok": False, "error": f"{est['tiles']} tiles exceeds the limit of {est['limit']} (raise tiles.download_max_tiles or reduce the area/zoom)"}
        job = {"active": True, "name": name, "south": south, "west": west, "north": north, "east": east,
               "zmin": zmin, "zmax": zmax, "total": est["tiles"], "done": 0, "skipped": 0, "failed": 0,
               "bytes": 0, "started": time.time(), "finished": None, "error": None, "cancelled": False}
        with self._lock:
            self.job = job
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, name="varmap-tiles", daemon=True)
        self._thread.start()
        return {"ok": True, "estimate": est}

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        job = self.job
        assert job is not None
        workers = max(1, min(4, int(self.cfg.get("tiles", "download_concurrency") or 2)))

        def one(t: Tuple[int, int, int]) -> None:
            if self._cancel.is_set():
                return
            z, x, y = t
            if self.store.has(z, x, y):
                with self._lock:
                    job["skipped"] += 1
                    job["done"] += 1
                return
            data = self.fetcher.fetch(z, x, y, polite=True)
            with self._lock:
                job["done"] += 1
                if data:
                    job["bytes"] += len(data)
                else:
                    job["failed"] += 1
            if data:
                self.store.put(z, x, y, data)

        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for _ in ex.map(one, region_tiles(job["south"], job["west"], job["north"], job["east"], job["zmin"], job["zmax"])):
                    if self._cancel.is_set():
                        break
            if not self._cancel.is_set():
                self.store.add_region(name=job["name"], south=job["south"], west=job["west"], north=job["north"],
                                      east=job["east"], zmin=job["zmin"], zmax=job["zmax"],
                                      tiles=job["done"] - job["failed"], bytes=job["bytes"],
                                      created_at=datetime.now(timezone.utc).isoformat(),
                                      source_url=self.cfg.get("tiles", "source_url"))
        except Exception as e:
            with self._lock:
                job["error"] = str(e)
            log.exception("region download failed: %s", e)
        finally:
            with self._lock:
                job["active"] = False
                job["cancelled"] = self._cancel.is_set()
                job["finished"] = time.time()
            log.info("region '%s' finished: %d/%d tiles, %d failed, %.1f MB", job["name"], job["done"], job["total"],
                     job["failed"], job["bytes"] / 1048576.0)
