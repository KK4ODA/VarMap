"""Entry point.  `python -m varmap` or the start_varmap.bat launcher."""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

# Allow `python varmap\__main__.py` as well as `python -m varmap`.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "varmap"  # noqa: A001

from . import __version__  # noqa: E402
from .runtime import Runtime, setup_logging  # noqa: E402

log = logging.getLogger("varmap.main")


def _port_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="varmap", description="VarMap - VarAC position mapping companion")
    ap.add_argument("--config", help="path to config.json (default: beside the app)")
    ap.add_argument("--port", type=int, help="web port (overrides config)")
    ap.add_argument("--host", help="bind address (default 127.0.0.1)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    ap.add_argument("--status", action="store_true", help="print VarAC discovery + database status and exit")
    ap.add_argument("--dump-windows", action="store_true", help="list VarAC's child windows (for GUI automation) and exit")
    ap.add_argument("--test-db", metavar="PATH", help="validate a VarAC database file and exit")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # VarAC captions contain symbols cp1252 cannot print
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.test_db:
        from .integration.varac_db import validate_database
        print(json.dumps(validate_database(args.test_db), indent=2))
        return 0
    if args.dump_windows:
        from .integration.varac_tx import dump_windows
        d = dump_windows()
        if not d.get("ok"):
            print("ERROR:", d.get("error"))
            return 1
        print(f"VarAC main window {d['main_hwnd']}: {d['title']}")
        for c in d["children"]:
            if c["visible"]:
                print(f"  {c['hwnd']:>10}  {c['class']:<40} {c['rect']}  {c['text']!r}")
        return 0

    if args.status:
        # Read-only diagnostics: never checks or touches the database's integrity, because
        # the main VarMap may be running on it.
        rt = Runtime(args.config, start_threads=False, check_integrity=False)
        setup_logging(None, logging.INFO)
        from .integration.varac_db import validate_database
        print(json.dumps({"varac": rt.vc.describe(), "database": validate_database(rt.vc.db_path()),
                          "counts": rt.repo.counts(), "own": rt.tracker.reader.describe()}, indent=2, default=str))
        return 0

    # The port check comes BEFORE the runtime is built: it is our "is another VarMap
    # running?" test, and the database integrity check must only run when the answer is no.
    from .config import Config
    pre = Config(args.config)
    host = args.host or pre.get("web", "host") or "127.0.0.1"
    port = args.port or int(pre.get("web", "port") or 5001)
    if _port_in_use("127.0.0.1", port):
        setup_logging(None, logging.INFO)
        log.error("Port %d is already in use. Is another VarMap running? Change web.port in config.json.", port)
        return 1

    rt = Runtime(args.config, start_threads=False, check_integrity=True)
    setup_logging(rt.cfg.log_path(), logging.DEBUG if args.verbose else logging.INFO)
    log.info("VarMap %s", __version__)
    if rt.repo.quarantined:
        log.error("The station database was damaged and has been moved to %s; rebuilding from VarAC.", rt.repo.quarantined)

    from .web.app import create_app
    app = create_app(rt)
    rt.start()

    url = f"http://127.0.0.1:{port}"
    log.info("Open %s", url)
    if not args.no_browser and rt.cfg.get("web", "open_browser"):
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)), daemon=True).start()

    try:
        try:
            from waitress import serve  # type: ignore
            serve(app, host=host, port=port, threads=16, ident="VarMap")
        except ImportError:
            log.warning("waitress not installed; using Flask's development server (pip install waitress)")
            import flask.cli
            flask.cli.show_server_banner = lambda *_: None
            app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        rt.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
