"""Offline user app.

Usage:
    python main.py download-map              # download Gainesville, FL (default)
    python main.py download-map --place "Ocala, Florida"
    python main.py download-map --bbox -82.42 29.59 -82.25 29.73 --max-zoom 15
    python main.py run                        # start the web interface

The map is a Protomaps vector basemap (OpenStreetMap data). `download-map` cuts
the requested area out of the latest daily Protomaps world build using HTTP
range requests and saves it as a single .pmtiles file, which the web interface
renders offline with Leaflet + protomaps-leaflet.
"""

import argparse
import datetime
import functools
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, render_template, send_file
from pmtiles.reader import MmapSource, Reader
from pmtiles.tile import zxy_to_tileid
from pmtiles.writer import Writer

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MAP_FILE = DATA_DIR / "map.pmtiles"
MAP_INFO_FILE = DATA_DIR / "map.json"

PROTOMAPS_BUILDS_URL = "https://build-metadata.protomaps.dev/builds.json"
PROTOMAPS_BUILD_URL = "https://build.protomaps.com/{key}"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Swamphacks2026-UserApp/0.2 (+https://github.com/NeedToLookAtMyPictures/Swamphacks2026)"

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


def http_get(url, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, response.read()


def geocode(place):
    query = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
    _, body = http_get(f"{NOMINATIM_URL}?{query}")
    results = json.loads(body)
    if not results:
        raise SystemExit(f"Could not find a place named {place!r}")
    south, north, west, east = map(float, results[0]["boundingbox"])
    return (west, south, east, north)


def latest_protomaps_build():
    """Return the URL of the newest daily Protomaps world build."""
    try:
        _, body = http_get(PROTOMAPS_BUILDS_URL)
        keys = re.findall(r"\d{8}\.pmtiles", body.decode())
        if keys:
            return PROTOMAPS_BUILD_URL.format(key=max(keys))
    except (urllib.error.URLError, OSError) as exc:
        print(f"Could not read the Protomaps build list ({exc}); probing recent dates")

    # Fall back to probing the last few days directly
    today = datetime.date.today()
    for days_back in range(10):
        key = (today - datetime.timedelta(days=days_back)).strftime("%Y%m%d") + ".pmtiles"
        url = PROTOMAPS_BUILD_URL.format(key=key)
        try:
            http_get(url, {"Range": "bytes=0-6"})
            return url
        except (urllib.error.URLError, OSError):
            continue
    raise SystemExit("Could not find a Protomaps build. Pass one with --source.")


def http_range_source(url):
    """A pmtiles byte source that reads a remote file with HTTP range requests."""

    @functools.lru_cache(maxsize=512)  # directories are re-read for every tile
    def get_bytes(offset, length):
        status, data = http_get(url, {"Range": f"bytes={offset}-{offset + length - 1}"})
        if status != 206:
            raise SystemExit(f"{url} does not support range requests (HTTP {status})")
        return data

    return get_bytes


def download_map(args):
    if args.bbox:
        bbox, name = tuple(args.bbox), args.place or "Custom area"
    elif args.place and args.place != DEFAULT_PLACE:
        bbox, name = geocode(args.place), args.place
    else:
        bbox, name = DEFAULT_BBOX, DEFAULT_PLACE

    source = args.source or latest_protomaps_build()
    if Path(source).exists():
        source_file = open(source, "rb")
        reader = Reader(MmapSource(source_file))
    else:
        reader = Reader(http_range_source(source))

    src_header = reader.header()
    max_zoom = min(args.max_zoom, src_header["max_zoom"])
    tiles = sorted(tiles_in_bbox(bbox, args.min_zoom, max_zoom), key=lambda t: zxy_to_tileid(*t))
    print(f"Extracting {name} (zoom {args.min_zoom}-{max_zoom}, bbox {bbox})")
    print(f"  source: {source}")
    print(f"  {len(tiles)} tiles to fetch")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = MAP_FILE.with_suffix(".pmtiles.part")
    written = total_bytes = 0
    with open(tmp_file, "wb") as f:
        writer = Writer(f)
        for i, (z, x, y) in enumerate(tiles, 1):
            data = reader.get(z, x, y)
            if data:  # tiles with nothing in them are absent from the source
                writer.write_tile(zxy_to_tileid(z, x, y), data)
                written += 1
                total_bytes += len(data)
            if i % 25 == 0 or i == len(tiles):
                print(f"  {i}/{len(tiles)} ({total_bytes / 1e6:.1f} MB)")

        if written == 0:
            f.close()
            tmp_file.unlink()
            raise SystemExit("No tiles found in that area.")

        min_lon, min_lat, max_lon, max_lat = bbox
        header = {
            "tile_type": src_header["tile_type"],
            "tile_compression": src_header["tile_compression"],
            "min_lon_e7": int(min_lon * 1e7),
            "min_lat_e7": int(min_lat * 1e7),
            "max_lon_e7": int(max_lon * 1e7),
            "max_lat_e7": int(max_lat * 1e7),
            "center_zoom": min(args.min_zoom + 3, max_zoom),
            "center_lon_e7": int((min_lon + max_lon) / 2 * 1e7),
            "center_lat_e7": int((min_lat + max_lat) / 2 * 1e7),
        }
        writer.finalize(header, reader.metadata())

    tmp_file.replace(MAP_FILE)
    MAP_INFO_FILE.write_text(json.dumps({
        "name": name,
        "source": source,
        "bbox": bbox,
        "center": [(min_lat + max_lat) / 2, (min_lon + max_lon) / 2],
        "min_zoom": args.min_zoom,
        "max_zoom": max_zoom,
    }, indent=2))
    print(f"Saved {MAP_FILE.relative_to(BASE_DIR)} ({MAP_FILE.stat().st_size / 1e6:.1f} MB). "
          "Start the app with `python main.py run`.")


# ---------------------------------------------------------------------------
# Web interface
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")


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
        print("No offline map found. Run `python main.py download-map` first.")
    app.run(host=args.host, port=args.port, debug=args.debug)


def main():
    parser = argparse.ArgumentParser(description="Offline user app")
    sub = parser.add_subparsers(dest="command")

    dl = sub.add_parser("download-map", help="download a map area for offline use")
    dl.add_argument("--place", default=DEFAULT_PLACE,
                    help=f'place name to look up (default: "{DEFAULT_PLACE}")')
    dl.add_argument("--bbox", nargs=4, type=float,
                    metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
                    help="explicit bounding box instead of a place lookup")
    dl.add_argument("--min-zoom", type=int, default=8)
    dl.add_argument("--max-zoom", type=int, default=15,
                    help="highest zoom to download (Protomaps data stops at 15; "
                         "the map still zooms further by scaling up)")
    dl.add_argument("--source",
                    help="Protomaps .pmtiles URL or local file (default: latest daily build)")
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
