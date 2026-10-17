"""Offline user app.

Usage:
    python download_locales.py map    # once, while online
    python main.py                    # start the web interface (works offline)

With an ESP32 running mesh/firmware/espnow_bridge, also sync over the mesh:
    python main.py --mesh-port /dev/ttyACM0 --mesh-demo

Two hikers on one computer (each with its own database, web port and board):
    python main.py --db data/alice.db --port 5001 --mesh-port /dev/ttyACM0 --mesh-demo
    python main.py --db data/bob.db --port 5002 --mesh-port /dev/ttyACM1 --mesh-demo
"""

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from flask import Flask, render_template, send_file

from api import api
from database import Database
from global_database import GlobalDatabase
from download_locales import DEFAULT_BBOX, MAP_FILE, MAP_INFO_FILE


# ---------------------------------------------------------------------------
# Web interface
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["DATABASE"] = Database()
app.config["GLOBAL_DATABASE"] = GlobalDatabase()
app.register_blueprint(api)


def load_map_info():
    if MAP_FILE.exists() and MAP_INFO_FILE.exists():
        return json.loads(MAP_INFO_FILE.read_text())
    min_lon, min_lat, max_lon, max_lat = DEFAULT_BBOX
    return {
        "name": None,
        "bbox": DEFAULT_BBOX,
        "center": [(min_lat + max_lat) / 2, (min_lon + max_lon) / 2],
        "min_zoom": 8,
        "max_zoom": 15,
    }


@app.route("/")
def index():
    return render_template("index.html", map_info=load_map_info())


@app.route("/map.pmtiles")
def map_file():
    # send_file handles the HTTP range requests protomaps-leaflet makes
    return send_file(MAP_FILE, mimetype="application/octet-stream", conditional=True)


def start_mesh(db, serial_port, demo):
    """Run a mesh node on this app's database in a background thread.

    The node starts once the device has a name (set up in the web interface),
    since the mesh identifies devices by it.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mesh"))
    from trailmesh import AppStore, Config, Node
    from trailmesh.espnow_serial import EspNowSerialLink

    def mesh_log(*msg):
        print("[mesh]", *msg, flush=True)

    def worker():
        while db.get_device() is None:
            time.sleep(1)
        link = EspNowSerialLink(serial_port, log=mesh_log)
        node = Node(
            link, AppStore(db), Config(always_on=demo),
            on_record=lambda r: mesh_log(f"received {r.kind.name.lower()} from the mesh"),
            on_delivered=lambda origin, seq: mesh_log(f"message {origin}/{seq} was delivered"),
            on_session_end=lambda s: mesh_log(f"sync with node {s.peer_id:08x} {'ok' if s.ok else 'failed'}"),
        )
        mesh_log(f"node {node.id:08x} running on {serial_port}")
        node.run_forever()

    threading.Thread(target=worker, daemon=True, name="mesh").start()


def run(args):
    if args.db:
        app.config["DATABASE"] = Database(args.db)
    if not MAP_FILE.exists():
        print("No offline map found. Run `python download_locales.py map` first.")
    app.config["DATABASE"].purge()
    if args.mesh_port:
        if args.debug:
            sys.exit("--mesh-port can't be combined with --debug (the reloader would open the board twice)")
        start_mesh(app.config["DATABASE"], args.mesh_port, args.mesh_demo)
    app.run(host=args.host, port=args.port, debug=args.debug)


def main():
    parser = argparse.ArgumentParser(description="Offline user app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--db", help="database file (default data/user.db); use one per hiker on a shared computer")
    parser.add_argument("--mesh-port", help="serial port of an ESP32 running the bridge firmware; syncs over the mesh")
    parser.add_argument("--mesh-demo", action="store_true", help="mesh radio always on (instant discovery)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
