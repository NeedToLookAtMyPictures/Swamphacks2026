"""Offline user app.

Usage:
    python download_locales.py map    # once, while online
    python main.py                    # start the web interface (works offline)
    python main.py --server https://example.com   # sync with this server when it's reachable
    python main.py --no-lan           # don't look for a server on the wifi
    python main.py --host 0.0.0.0 --https   # on a Raspberry Pi, for a phone to connect to
"""

import argparse
import ipaddress
import json
import os
import socket
import subprocess

from flask import Flask, render_template, send_file

import server_sync
from api import api
from database import Database
from global_database import GlobalDatabase
from download_locales import DATA_DIR, DEFAULT_BBOX, MAP_FILE, MAP_INFO_FILE

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


def run(args):
    if not MAP_FILE.exists():
        print("No offline map found. Run `python download_locales.py map` first.")
    app.config["DATABASE"].purge()
    finder = server_sync.ServerFinder(args.server, lan=not args.no_lan)
    app.config["SERVER"] = finder
    # With --debug the reloader runs this twice; only sync from the process that serves
    if (args.server or not args.no_lan) and (
            not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        print(f"Syncing with {finder.describe()} whenever it can be reached")
        server_sync.start(app.config["DATABASE"], finder)
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
    parser.add_argument("--server", default=os.environ.get("SERVER_URL"),
                        help="server to sync with whenever it can be reached "
                             "(default: $SERVER_URL). Without it, or when it can't be "
                             "reached, a server on the same wifi is used")
    parser.add_argument("--no-lan", action="store_true",
                        help="don't look for a server on the wifi")
    parser.add_argument("--https", action="store_true",
                        help="serve over HTTPS (needed for GPS when a phone opens the app "
                             "from another device, e.g. a Raspberry Pi over Bluetooth)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
