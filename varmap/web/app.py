"""Flask routes.  JSON over HTTP on localhost; the browser does the mapping."""
from __future__ import annotations

import base64
import logging
import os
from datetime import timedelta
from typing import Any, Dict, Optional

from flask import Flask, Response, abort, jsonify, render_template, request

from ..domain.geo import bearing_deg, haversine_km, km_to_mi
from ..domain.grid import grid_bounds
from ..domain.staleness import classify
from ..domain.timeparse import iso_utc, now_utc, parse_iso
from ..integration.varac_db import validate_database
from ..integration.varac_tx import dump_windows

log = logging.getLogger("varmap.web")

# 1x1 transparent PNG used when a tile is unavailable offline.
_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def create_app(rt) -> Flask:
    here = os.path.dirname(os.path.abspath(__file__))
    app = Flask("varmap", static_folder=os.path.join(here, "static"), template_folder=os.path.join(here, "templates"))
    app.config["JSON_SORT_KEYS"] = False

    @app.before_request
    def _csrf_guard():
        # Localhost-only server; a custom header defeats cross-site form posts.
        if request.method in ("POST", "PUT", "DELETE") and request.headers.get("X-Requested-With") != "VarMap":
            abort(403)

    # -- helpers ---------------------------------------------------------------
    def own_latlon() -> Optional[Dict[str, float]]:
        fix = rt.tracker.current()
        if fix:
            return {"lat": fix.lat, "lon": fix.lon}
        last = rt.repo.own_position_latest()
        if last:
            return {"lat": last["lat"], "lon": last["lon"]}
        return None

    def decorate(st: Dict[str, Any], now, own: Optional[Dict[str, float]], stale_cfg: Dict[str, Any], mi: bool) -> Dict[str, Any]:
        pt = parse_iso(st.get("position_time"))
        lh = parse_iso(st.get("last_heard"))
        st["position_age_s"] = (now - pt).total_seconds() if pt else None
        st["heard_age_s"] = (now - lh).total_seconds() if lh else None
        st["state"] = classify(st["position_age_s"], stale_cfg) if pt else "none"
        st["heard_state"] = classify(st["heard_age_s"], stale_cfg) if lh else "none"
        if own and st.get("lat") is not None:
            km = haversine_km(own["lat"], own["lon"], st["lat"], st["lon"])
            st["distance_km"] = round(km, 1)
            st["distance_display"] = f"{km_to_mi(km):.0f} mi" if mi else f"{km:.0f} km"
            st["bearing_deg"] = round(bearing_deg(own["lat"], own["lon"], st["lat"], st["lon"]))
        else:
            st["distance_km"] = None
            st["distance_display"] = None
            st["bearing_deg"] = None
        if st.get("grid"):
            st["grid_bounds"] = grid_bounds(st["grid"])
        return st

    def units_mi() -> bool:
        u = rt.cfg.get("map", "units") or "auto"
        if u == "auto":
            u = rt.vc.distance_unit()
        return str(u).upper().startswith("MI")

    # -- pages -------------------------------------------------------------------
    @app.route("/")
    def index():
        # Cache-buster: version + newest mtime of the static assets, so style/script edits always show.
        static_dir = os.path.join(here, "static")
        try:
            stamp = max(int(os.path.getmtime(os.path.join(static_dir, f))) for f in ("app.css", "app.js"))
        except OSError:
            stamp = 0
        return render_template("index.html", v=f"{__import__('varmap').__version__}-{stamp}")

    # -- health / config -----------------------------------------------------------
    @app.route("/api/health")
    def api_health():
        return jsonify(rt.health())

    @app.route("/api/config", methods=["GET"])
    def api_config_get():
        return jsonify(rt.cfg.snapshot())

    @app.route("/api/config", methods=["POST"])
    def api_config_set():
        patch = request.get_json(force=True, silent=True) or {}
        if not isinstance(patch, dict):
            return jsonify({"ok": False, "error": "expected a JSON object"}), 400
        rt.apply_config(patch)
        return jsonify({"ok": True, "config": rt.cfg.snapshot()})

    @app.route("/api/test_db", methods=["POST"])
    def api_test_db():
        body = request.get_json(force=True, silent=True) or {}
        path = (body.get("path") or "").strip() or rt.vc.db_path()
        return jsonify(validate_database(path))

    @app.route("/api/graywolf/test", methods=["POST"])
    def api_graywolf_test():
        body = request.get_json(force=True, silent=True) or {}
        from ..integration.graywolf import GraywolfClient
        g = rt.cfg.get("graywolf") or {}
        c = GraywolfClient(body.get("url") or g.get("url"), body.get("username") or g.get("username"),
                           body.get("password") or g.get("password"))
        return jsonify(c.test())

    @app.route("/api/poll_now", methods=["POST"])
    def api_poll_now():
        rt.poller.wake()
        return jsonify({"ok": True})

    @app.route("/api/windows")
    def api_windows():
        return jsonify(dump_windows())

    # -- stations ---------------------------------------------------------------------
    @app.route("/api/stations")
    def api_stations():
        now = now_utc()
        since = request.args.get("since") or None
        own = own_latlon()
        stale_cfg = rt.cfg.get("staleness")
        mi = units_mi()
        rows = rt.repo.stations(since_iso=since, include_hidden=bool(request.args.get("include_hidden")))
        bands = rt.repo.recent_bands(hours=6.0)
        out = []
        for r in rows:
            r["bands_recent"] = bands.get(r["callsign"], [])
            out.append(decorate(r, now, own, stale_cfg, mi))
        return jsonify({"now": iso_utc(now), "stations": out, "delta": bool(since),
                        "data_version": rt.data_version(), "own": own})

    @app.route("/api/station/<callsign>")
    def api_station(callsign: str):
        st = rt.repo.station(callsign)
        if not st:
            abort(404)
        now = now_utc()
        decorate(st, now, own_latlon(), rt.cfg.get("staleness"), units_mi())
        st["bands_recent"] = rt.repo.recent_bands(hours=6.0).get(st["callsign"], [])
        days = float(request.args.get("days") or 30)
        st["positions"] = rt.repo.positions(callsign, limit=500, since_iso=iso_utc(now - timedelta(days=days)))
        st["heard"] = rt.repo.heard(callsign, limit=40)
        return jsonify(st)

    @app.route("/api/station/<callsign>", methods=["POST"])
    def api_station_update(callsign: str):
        body = request.get_json(force=True, silent=True) or {}
        ok = rt.repo.set_station_user_fields(callsign, **{k: body[k] for k in ("notes", "is_favorite", "is_hidden") if k in body})
        return jsonify({"ok": ok})

    @app.route("/api/station/<callsign>/track")
    def api_track(callsign: str):
        days = float(request.args.get("days") or 7)
        pts = rt.repo.positions(callsign, limit=5000, since_iso=iso_utc(now_utc() - timedelta(days=days)))
        pts.reverse()
        # Collapse consecutive identical grids (rendering decision, not storage).
        collapsed = []
        for p in pts:
            if collapsed and collapsed[-1]["grid"] and collapsed[-1]["grid"] == p["grid"]:
                collapsed[-1]["count"] += 1
                collapsed[-1]["until"] = p["heard_at"]
                continue
            p["count"] = 1
            p["until"] = p["heard_at"]
            collapsed.append(p)
        return jsonify({"callsign": callsign.upper(), "points": collapsed, "raw_count": len(pts)})

    @app.route("/api/broadcasts")
    def api_broadcasts():
        return jsonify({"broadcasts": rt.repo.recent_broadcasts(limit=int(request.args.get("limit") or 50))})

    # -- own position -------------------------------------------------------------------
    @app.route("/api/own")
    def api_own():
        d = rt.tracker.describe()
        hours = float(request.args.get("hours") or 0)
        if hours > 0:
            d["history"] = rt.repo.own_position_history(iso_utc(now_utc() - timedelta(hours=hours)))
        return jsonify(d)

    # -- beacon ----------------------------------------------------------------------------
    @app.route("/api/beacon")
    def api_beacon():
        s = rt.beacon.snapshot()
        s["recent"] = rt.repo.beacon_tx_recent(limit=30)
        s["preview"] = rt.beacon.preview()
        return jsonify(s)

    @app.route("/api/beacon/enable", methods=["POST"])
    def api_beacon_enable():
        body = request.get_json(force=True, silent=True) or {}
        rt.beacon.set_enabled(bool(body.get("enabled")), body.get("dry_run"))
        return jsonify({"ok": True, "state": rt.beacon.snapshot()})

    @app.route("/api/beacon/send_now", methods=["POST"])
    def api_beacon_send_now():
        return jsonify(rt.beacon.send_now())

    @app.route("/api/beacon/rehearse", methods=["POST"])
    def api_beacon_rehearse():
        return jsonify(rt.beacon.rehearse())

    @app.route("/api/beacon/reset", methods=["POST"])
    def api_beacon_reset():
        rt.beacon.reset_schedule()
        return jsonify({"ok": True})

    # -- tiles ---------------------------------------------------------------------------------
    @app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
    def tile(z: int, x: int, y: int):
        data = rt.tiles.get(z, x, y)
        if data is None and rt.cfg.get("tiles", "online_fetch"):
            data = rt.tile_fetcher.fetch(z, x, y)
            if data:
                rt.tiles.put(z, x, y, data)
        if data is None:
            return Response(_BLANK_PNG, mimetype="image/png", status=404, headers={"Cache-Control": "no-store"})
        return Response(data, mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.route("/api/tiles/stats")
    def api_tiles_stats():
        s = rt.tiles.stats()
        s["regions"] = rt.tiles.regions()
        s["download"] = rt.downloader.status()
        s["source_url"] = rt.cfg.get("tiles", "source_url")
        s["online_fetch"] = bool(rt.cfg.get("tiles", "online_fetch"))
        s["fetch_errors"] = rt.tile_fetcher.last_error
        return jsonify(s)

    def _bbox(body: Dict[str, Any]):
        try:
            s, w, n, e = float(body["south"]), float(body["west"]), float(body["north"]), float(body["east"])
            zmin, zmax = int(body.get("zmin", 5)), int(body.get("zmax", 12))
        except (KeyError, TypeError, ValueError):
            return None
        zmax = min(zmax, int(rt.cfg.get("tiles", "max_zoom") or 17))
        zmin = max(0, min(zmin, zmax))
        return s, w, n, e, zmin, zmax

    @app.route("/api/tiles/estimate", methods=["POST"])
    def api_tiles_estimate():
        bb = _bbox(request.get_json(force=True, silent=True) or {})
        if not bb:
            return jsonify({"ok": False, "error": "bad bbox"}), 400
        return jsonify(rt.downloader.estimate(*bb))

    @app.route("/api/tiles/download", methods=["POST"])
    def api_tiles_download():
        body = request.get_json(force=True, silent=True) or {}
        bb = _bbox(body)
        if not bb:
            return jsonify({"ok": False, "error": "bad bbox"}), 400
        return jsonify(rt.downloader.start(body.get("name") or "region", *bb))

    @app.route("/api/tiles/cancel", methods=["POST"])
    def api_tiles_cancel():
        rt.downloader.cancel()
        return jsonify({"ok": True})

    @app.route("/api/tiles/region/<int:rid>", methods=["DELETE"])
    def api_tiles_region_delete(rid: int):
        purge = request.args.get("purge") == "1"
        return jsonify({"ok": True, "tiles_removed": rt.tiles.delete_region(rid, purge)})

    return app
