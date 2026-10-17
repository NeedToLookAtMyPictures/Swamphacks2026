"""Offline user app.

Usage:
    python download_locales.py map    # once, while online
    python main.py                    # start the web interface (works offline)
    python main.py --server https://example.com   # sync with another server (default: DEFAULT_SERVER_URLS)
    python main.py --server https://a.example,http://10.0.0.5:8000   # several, tried in order
    python main.py --server ""        # no server address, only look on the wifi
    python main.py --no-lan           # don't look for a server on the wifi
    python main.py --host 0.0.0.0 --https   # on a Raspberry Pi, for a phone to connect to

With an ESP32 running mesh/firmware/espnow_bridge, also sync over the mesh:
    python main.py --mesh-port /dev/ttyACM0 --mesh-demo

On an Android phone in Termux, with the ESP32 plugged into the phone over USB:
    python main.py --phone --mesh-demo
(it reruns itself through termux-usb to get the board: accept the dialog; then
open http://127.0.0.1:5000 in the phone's browser. See mesh/docs/transport.md.)

Two hikers on one computer (each with its own database, web port and board):
    python main.py --db data/alice.db --port 5001 --mesh-port /dev/ttyACM0 --mesh-demo
    python main.py --db data/bob.db --port 5002 --mesh-port /dev/ttyACM1 --mesh-demo
"""

import argparse
import ipaddress
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, render_template, send_file

import server_sync
from api import api, load_map_info
from database import Database
from global_database import GlobalDatabase
from download_locales import DATA_DIR, MAP_FILE

# The demo server (the laptop running application/server), tried in this order.
# PUBLIC_SERVER_URL is its address on the internet (server/serve_public.sh prints
# it), so it works from any network; LAN_SERVER_URL is its wifi address, which
# can change when it reconnects: check with `ip -4 addr show` and update it here.
PUBLIC_SERVER_URL = "https://heuslerene.com/server"
LAN_SERVER_URL = "http://10.138.191.107:8000"
DEFAULT_SERVER_URLS = ",".join(u for u in (PUBLIC_SERVER_URL, LAN_SERVER_URL) if u)

CERT_FILE = DATA_DIR / "cert.pem"
KEY_FILE = DATA_DIR / "key.pem"


# ---------------------------------------------------------------------------
# Web interface
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["DATABASE"] = Database()
app.config["SERVER"] = None  # a server_sync.ServerFinder, set in run()
app.config["GLOBAL_DATABASE"] = GlobalDatabase()
app.register_blueprint(api)


@app.route("/")
def index():
    return render_template("index.html", map_info=load_map_info())


@app.route("/map.pmtiles")
def map_file():
    # send_file handles the HTTP range requests protomaps-leaflet makes
    return send_file(MAP_FILE, mimetype="application/octet-stream", conditional=True)


# ---------------------------------------------------------------------------
# HTTPS
# ---------------------------------------------------------------------------
# Browsers only give a page the GPS position on localhost or HTTPS. When the app
# runs on a Raspberry Pi and a phone opens it over Bluetooth (http://<pi-ip>),
# that's neither, so the Pi has to serve HTTPS. There's no internet to get a real
# certificate, so we make a self-signed one; the phone shows a warning once.

def local_addresses():
    """Names and IPs this machine may be reached by, for the certificate."""
    hostname = socket.gethostname()
    names = {"localhost", hostname, f"{hostname}.local"}
    ips = {"127.0.0.1"}
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout
        ips.update(ip for ip in out.split() if ipaddress.ip_address(ip).version == 4)
    except (OSError, ValueError):
        pass
    return sorted(names), sorted(ips)


def ensure_certificate():
    """Create data/cert.pem and data/key.pem once; reused so phones only accept it once."""
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    names, ips = local_addresses()
    san = ",".join([f"DNS:{n}" for n in names] + [f"IP:{ip}" for ip in ips])
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
         "-days", "3650", "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
         "-subj", f"/CN={names[0]}", "-addext", f"subjectAltName={san}"],
        check=True, capture_output=True)
    print(f"Created a self-signed certificate in {DATA_DIR} for {', '.join(names + ips)}")


def start_mesh(db, serial_port, demo, board_kind="espnow", radio="auto"):
    """Run a mesh node on this app's database in a background thread.

    The board is opened right away, here on the main thread: on Android that
    reruns the whole program under termux-usb, which has to happen before the
    web server starts. The node starts once the device has a name (set up in
    the web interface), since the mesh identifies devices by it.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mesh"))
    from trailmesh import AppStore
    from trailmesh.espnow_serial import LinkLost
    from trailmesh.radio import build_node, open_board
    from trailmesh.transport import TransportError

    def mesh_log(*msg):
        print("[mesh]", *msg, flush=True)

    try:
        board = open_board(board_kind, serial_port, log=mesh_log)
    except TransportError as e:
        sys.exit(f"can't open the mesh board: {e}")
    board.set_radio(False)  # the node turns on the radio it uses when it starts

    def worker():
        while db.get_device() is None:
            time.sleep(1)
        node = build_node(
            board, AppStore(db), radio=radio, config=dict(always_on=demo),
            on_record=lambda r: mesh_log(f"received {r.kind.name.lower()} from the mesh"),
            on_delivered=lambda origin, seq: mesh_log(f"message {origin}/{seq} was delivered"),
            on_session_end=lambda s: mesh_log(
                f"sync with node {s.peer_id:08x} over {'ESP-NOW' if s.fast or node.link is board else 'LoRa'} "
                f"{'ok' if s.ok else 'failed'}"),
        )
        mesh_log(f"node {node.id:08x} running on {serial_port} ({board_kind}, radio {radio})")
        try:
            node.run_forever()
        except LinkLost as e:
            # Android: the board was unplugged and only a new program run can get it back.
            # The web interface keeps working without the mesh.
            mesh_log(f"{e}; the mesh is off until then")

    threading.Thread(target=worker, daemon=True, name="mesh").start()


class HideRoutineRequests(logging.Filter):
    """The page re-reads the database every second or so; logging each of those
    requests buries the sync and [mesh] lines. Writes and errors are still shown."""

    def filter(self, record):
        # werkzeug colours some lines (304s cyan), which puts escape codes inside the quotes
        message = re.sub(r"\x1b\[[0-9;]*m", "", record.getMessage())
        return not ('"GET ' in message and ('" 200 ' in message or '" 304 ' in message))


def run(args):
    if not args.log_requests:
        logging.getLogger("werkzeug").addFilter(HideRoutineRequests())
    if args.db:
        app.config["DATABASE"] = Database(args.db)
    if not MAP_FILE.exists():
        print("No offline map found. Run `python download_locales.py map` first.")
    app.config["DATABASE"].purge()
    # Before anything else starts: on Android this reruns the program (see start_mesh)
    if args.phone and not args.mesh_port:
        args.mesh_port = "termux-usb"
    if args.mesh_port:
        if args.debug:
            sys.exit("--mesh-port can't be combined with --debug (the reloader would open the board twice)")
        start_mesh(app.config["DATABASE"], args.mesh_port, args.mesh_demo, args.mesh_board, args.mesh_radio)
    finder = server_sync.ServerFinder(args.server, lan=not args.no_lan)
    app.config["SERVER"] = finder
    # With --debug the reloader runs this twice; only sync from the process that serves
    if (args.server or not args.no_lan) and (
            not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        print(f"Syncing with {finder.describe()} whenever it can be reached")
        server_sync.start(app.config["DATABASE"], finder, app.config["GLOBAL_DATABASE"], DATA_DIR)
    ssl_context = None
    if args.https:
        ensure_certificate()
        ssl_context = (str(CERT_FILE), str(KEY_FILE))
    app.run(host=args.host, port=args.port, debug=args.debug, ssl_context=ssl_context)


def main():
    parser = argparse.ArgumentParser(description="Offline user app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--log-requests", action="store_true",
                        help="also log every successful GET (hidden by default: the page polls)")
    parser.add_argument("--server", default=os.environ.get("SERVER_URL", DEFAULT_SERVER_URLS),
                        help="servers to sync with whenever one can be reached, comma-separated "
                             f"and tried in order (default: $SERVER_URL, else {DEFAULT_SERVER_URLS}; "
                             "\"\" for none). When none can be reached, a server on the same "
                             "wifi is used")
    parser.add_argument("--no-lan", action="store_true",
                        help="don't look for a server on the wifi")
    parser.add_argument("--https", action="store_true",
                        help="serve over HTTPS (needed for GPS when a phone opens the app "
                             "from another device, e.g. a Raspberry Pi over Bluetooth)")
    parser.add_argument("--db", help="database file (default data/user.db); use one per hiker on a shared computer")
    parser.add_argument("--mesh-port", help="the ESP32 running the bridge firmware, syncs over the mesh: "
                             "auto, COM3, /dev/ttyACM0, or on Android termux-usb or /dev/bus/usb/...")
    parser.add_argument("--phone", action="store_true",
                        help="running on an Android phone in Termux: reach the board through termux-usb "
                             "(the default --mesh-port then) instead of a serial port")
    parser.add_argument("--mesh-demo", action="store_true", help="mesh radio always on (instant discovery)")
    parser.add_argument("--mesh-board", choices=("espnow", "heltec"), default="espnow",
                        help="espnow: an ESP32 with espnow_bridge; heltec: a Heltec LoRa 32 V3 with heltec_bridge")
    parser.add_argument("--mesh-radio", choices=("auto", "lora", "espnow"), default="auto",
                        help="heltec only. auto: LoRa, switching to ESP-NOW within 60 m; lora / espnow: that radio only")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
