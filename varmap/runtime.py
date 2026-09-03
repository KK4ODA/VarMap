"""Wires the components together and owns the background threads."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from typing import Any, Dict, Optional

from . import __version__
from .config import Config
from .integration.varac_config import VaracConfig
from .services.beacon import BeaconService
from .services.graywolf_poller import GraywolfPoller
from .services.graywolf_tx import GraywolfTx
from .services.own_position import OwnPositionTracker
from .services.poller import Poller
from .services.tiles import RegionDownloader, TileFetcher, TileStore
from .services.updater import Updater
from .storage.repository import Repository

log = logging.getLogger("varmap")


def setup_logging(log_path: Optional[str] = None, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if log_path:
        try:
            fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            log.warning("cannot open log file %s: %s", log_path, e)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("waitress").setLevel(logging.WARNING)


class Runtime:
    def __init__(self, config_path: Optional[str] = None, start_threads: bool = True,
                 check_integrity: bool = False) -> None:
        self.cfg = Config(config_path)
        if not os.path.isfile(self.cfg.path):
            self.cfg.save()
        self.vc = VaracConfig(self.cfg)
        # check_integrity is True only for the main server, after the port check has
        # shown that no other VarMap is running (see __main__).
        self.repo = Repository(self.cfg.db_path(), check_integrity=check_integrity)
        self.tiles = TileStore(self.cfg.tiles_path())
        self.tile_fetcher = TileFetcher(self.cfg)
        self.downloader = RegionDownloader(self.tiles, self.tile_fetcher, self.cfg)
        self.poller = Poller(self)
        self.graywolf = GraywolfPoller(self)
        self.graywolf_tx = GraywolfTx(self)
        self.tracker = OwnPositionTracker(self)
        self.beacon = BeaconService(self)
        self.updater = Updater(self)
        self._data_version = 0
        self._lock = threading.Lock()
        self.started = False
        if start_threads:
            self.start()

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        self.poller.start()
        self.graywolf.start()
        self.graywolf_tx.start()
        self.tracker.start()
        self.beacon.start()
        self.updater.start()
        log.info("VarMap %s runtime started; data dir %s", __version__, self.cfg.data_dir())

    def stop(self) -> None:
        self.poller.stop()
        self.graywolf.stop()
        self.graywolf_tx.stop()
        self.tracker.stop()
        self.beacon.stop()
        self.updater.stop()

    def on_new_data(self, n: int) -> None:
        with self._lock:
            self._data_version += 1

    def data_version(self) -> int:
        with self._lock:
            return self._data_version

    def apply_config(self, patch: Dict[str, Any]) -> None:
        self.cfg.update(patch)
        self.poller.wake()
        self.graywolf.wake()
        self.tracker.wake()

    def health(self) -> Dict[str, Any]:
        """Never raises: a failing section reports its error instead of taking the UI down."""
        def safe(fn, fallback):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                log.debug("health section failed: %s", e)
                return {**fallback, "error": str(e)} if isinstance(fallback, dict) else fallback

        units = self.cfg.get("map", "units") or "auto"
        if units == "auto":
            units = safe(self.vc.distance_unit, "KM")
        return {
            "version": __version__,
            "varac": safe(self.vc.describe, {}),
            "poller": safe(self.poller.snapshot, {"status": "error", "connected": False}),
            "graywolf": safe(self.graywolf.snapshot, {"enabled": False, "connected": False}),
            "graywolf_tx": safe(self.graywolf_tx.snapshot, {}),
            "updates": safe(self.updater.snapshot, {"current": __version__, "available": False}),
            "own": safe(self.tracker.describe, {"fix": None}),
            "beacon": safe(self.beacon.snapshot, {"enabled": False}),
            "counts": safe(self.repo.counts, {}),
            "tiles": {"online_fetch": bool(self.cfg.get("tiles", "online_fetch")),
                      "fetched": self.tile_fetcher.fetched, "failed": self.tile_fetcher.failed,
                      "last_error": self.tile_fetcher.last_error, "download": self.downloader.status()},
            "units": units,
            "data_version": self.data_version(),
        }
