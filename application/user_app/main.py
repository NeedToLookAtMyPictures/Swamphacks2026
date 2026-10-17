"""Offline user app.

Usage:
    python main.py download-map              # download Gainesville, FL tiles (default)
    python main.py download-map --place "Ocala, Florida"
    python main.py download-map --bbox -82.42 29.59 -82.25 29.73 --max-zoom 16
    python main.py run                        # start the web interface
"""

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, abort, render_template, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TILES_DIR = DATA_DIR / "tiles"
MAP_INFO_FILE = DATA_DIR / "map.json"

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Swamphacks2026-UserApp/0.1 (offline demo)"

# Gainesville, Florida (min_lon, min_lat, max_lon, max_lat)
DEFAULT_PLACE = "Gainesville, Florida"
DEFAULT_BBOX = (-82.42, 29.59, -82.25, 29.73)


# ---------------------------------------------------------------------------
# Map download
# ---------------------------------------------------------------------------

def lonlat_to_tile(lon, lat, zoom):
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_in_bbox(bbox, min_zoom, max_zoom):
    min_lon, min_lat, max_lon, max_lat = bbox
    for z in range(min_zoom, max_zoom + 1):
        x0, y0 = lonlat_to_tile(min_lon, max_lat, z)
        x1, y1 = lonlat_to_tile(max_lon, min_lat, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def http_get(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def geocode(place):
    query = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
    results = json.loads(http_get(f"{NOMINATIM_URL}?{query}"))
    if not results:
        raise SystemExit(f"Could not find a place named {place!r}")
    south, north, west, east = map(float, results[0]["boundingbox"])
    return (west, south, east, north)


def download_map(args):
    if args.bbox:
        bbox, name = tuple(args.bbox), args.place or "Custom area"
    elif args.place and args.place != DEFAULT_PLACE:
        bbox, name = geocode(args.place), args.place
    else:
        bbox, name = DEFAULT_BBOX, DEFAULT_PLACE

    tiles = list(tiles_in_bbox(bbox, args.min_zoom, args.max_zoom))
    print(f"Downloading {len(tiles)} tiles for {name} "
          f"(zoom {args.min_zoom}-{args.max_zoom}, bbox {bbox})")

    downloaded = skipped = failed = 0
    for i, (z, x, y) in enumerate(tiles, 1):
        path = TILES_DIR / str(z) / str(x) / f"{y}.png"
        if path.exists() and not args.force:
            skipped += 1
            continue
        try:
            data = http_get(args.tile_url.format(z=z, x=x, y=y))
        except Exception as exc:
            failed += 1
            print(f"  failed {z}/{x}/{y}: {exc}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        downloaded += 1
        if i % 50 == 0 or i == len(tiles):
            print(f"  {i}/{len(tiles)}")
        time.sleep(args.delay)  # be polite to the tile server

    print(f"Done: {downloaded} downloaded, {skipped} already present, {failed} failed.")
    if downloaded + skipped == 0:
        raise SystemExit("No tiles available; check your internet connection and try again.")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    min_lon, min_lat, max_lon, max_lat = bbox
    MAP_INFO_FILE.write_text(json.dumps({
        "name": name,
        "bbox": bbox,
        "center": [(min_lat + max_lat) / 2, (min_lon + max_lon) / 2],
        "min_zoom": args.min_zoom,
        "max_zoom": args.max_zoom,
    }, indent=2))
    print(f"Saved map info to {MAP_INFO_FILE.relative_to(BASE_DIR)}. "
          "Start the app with `python main.py run`.")


# ---------------------------------------------------------------------------
# Web interface
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")


def load_map_info():
    if MAP_INFO_FILE.exists():
        return json.loads(MAP_INFO_FILE.read_text())
    min_lon, min_lat, max_lon, max_lat = DEFAULT_BBOX
    return {
        "name": None,
        "bbox": DEFAULT_BBOX,
        "center": [(min_lat + max_lat) / 2, (min_lon + max_lon) / 2],
        "min_zoom": 10,
        "max_zoom": 16,
    }


@app.route("/")
def index():
    return render_template("index.html", map_info=load_map_info())


@app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def tile(z, x, y):
    tile_dir = TILES_DIR / str(z) / str(x)
    if not (tile_dir / f"{y}.png").exists():
        abort(404)
    return send_from_directory(tile_dir, f"{y}.png")


def run(args):
    if not MAP_INFO_FILE.exists():
        print("No offline map found. Run `python main.py download-map` first.")
    app.run(host=args.host, port=args.port, debug=args.debug)


def main():
    parser = argparse.ArgumentParser(description="Offline user app")
    sub = parser.add_subparsers(dest="command")

    dl = sub.add_parser("download-map", help="download map tiles for offline use")
    dl.add_argument("--place", default=DEFAULT_PLACE,
                    help=f'place name to look up (default: "{DEFAULT_PLACE}")')
    dl.add_argument("--bbox", nargs=4, type=float,
                    metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
                    help="explicit bounding box instead of a place lookup")
    dl.add_argument("--min-zoom", type=int, default=10)
    dl.add_argument("--max-zoom", type=int, default=16)
    dl.add_argument("--tile-url", default=TILE_URL)
    dl.add_argument("--delay", type=float, default=0.1,
                    help="seconds to wait between tile requests")
    dl.add_argument("--force", action="store_true", help="re-download existing tiles")
    dl.set_defaults(func=download_map)

    rn = sub.add_parser("run", help="start the web interface")
    rn.add_argument("--host", default="127.0.0.1")
    rn.add_argument("--port", type=int, default=5000)
    rn.add_argument("--debug", action="store_true")
    rn.set_defaults(func=run)

    args = parser.parse_args()
    if args.command is None:
        args = parser.parse_args(["run"])
    args.func(args)


if __name__ == "__main__":
    main()
