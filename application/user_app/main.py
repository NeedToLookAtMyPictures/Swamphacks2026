"""Offline user app.

Usage:
    python download_locales.py map    # once, while online
    python main.py                    # start the web interface (works offline)
"""

import argparse
import json

from flask import Flask, render_template, send_file

from api import api
from database import Database
from download_locales import DEFAULT_BBOX, MAP_FILE, MAP_INFO_FILE


# ---------------------------------------------------------------------------
# Web interface
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["DATABASE"] = Database()
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


def run(args):
    if not MAP_FILE.exists():
        print("No offline map found. Run `python download_locales.py map` first.")
    app.config["DATABASE"].purge()
    app.run(host=args.host, port=args.port, debug=args.debug)


def main():
    parser = argparse.ArgumentParser(description="Offline user app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
