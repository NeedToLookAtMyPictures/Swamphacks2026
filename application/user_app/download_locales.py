"""Download data the user app needs while offline.

Run this once while you have internet:
    python download_locales.py map            # Gainesville to Hawthorne, FL basemap (default)
    python download_locales.py map --place "Ocala, Florida"
    python download_locales.py map --bbox -82.42 29.59 -82.25 29.73 --max-zoom 15

Everything is saved under data/. Default locale information (trails,
interesting locations, ...) will be downloaded from here too, each as its own
subcommand.

The map is a Protomaps vector basemap (OpenStreetMap data). `map` cuts the
requested area out of the latest daily Protomaps world build using HTTP range
requests and saves it as data/maps/<name>.pmtiles, next to the maps the
server sends (see maps.py).
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

from pmtiles.reader import MmapSource, Reader
from pmtiles.tile import zxy_to_tileid
from pmtiles.writer import Writer

from maps import map_files

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MAPS_DIR = DATA_DIR / "maps"  # the offline maps, see maps.py

PROTOMAPS_BUILDS_URL = "https://build-metadata.protomaps.dev/builds.json"
PROTOMAPS_BUILD_URL = "https://build.protomaps.com/{key}"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Swamphacks2026-UserApp/0.2 (+https://github.com/NeedToLookAtMyPictures/Swamphacks2026)"

# Gainesville east to Hawthorne, Florida, along the Gainesville-Hawthorne Trail
# (min_lon, min_lat, max_lon, max_lat)
DEFAULT_PLACE = "Gainesville to Hawthorne, Florida"
DEFAULT_BBOX = (-82.42, 29.56, -82.05, 29.73)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def http_get(url, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, response.read()


def geocode(place):
    """Look up a place name and return its bbox (min_lon, min_lat, max_lon, max_lat)."""
    query = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
    _, body = http_get(f"{NOMINATIM_URL}?{query}")
    results = json.loads(body)
    if not results:
        raise SystemExit(f"Could not find a place named {place!r}")
    south, north, west, east = map(float, results[0]["boundingbox"])
    return (west, south, east, north)


def resolve_area(args):
    """Return (bbox, name) from the --bbox / --place arguments."""
    if args.bbox:
        return tuple(args.bbox), args.place or "Custom area"
    if args.place and args.place != DEFAULT_PLACE:
        return geocode(args.place), args.place
    return DEFAULT_BBOX, DEFAULT_PLACE


def add_area_arguments(parser):
    parser.add_argument("--place", default=DEFAULT_PLACE,
                        help=f'place name to look up (default: "{DEFAULT_PLACE}")')
    parser.add_argument("--bbox", nargs=4, type=float,
                        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
                        help="explicit bounding box instead of a place lookup")


# ---------------------------------------------------------------------------
# Map
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


def open_source(source):
    """A pmtiles Reader for a Protomaps build: a URL (None: the latest daily build) or a local file."""
    source = source or latest_protomaps_build()
    if Path(source).exists():
        return Reader(MmapSource(open(source, "rb"))), source
    return Reader(http_range_source(source)), source


def extract_map(reader, source, tiles, bbox, name, min_zoom, max_zoom, map_file, info_file,
                trail=None):
    """Copy `tiles` [(z, x, y)] from reader into map_file, and write its info_file
    (see maps.py; trail is the id of the global trail the map is for, if any)."""
    tiles = sorted(tiles, key=lambda t: zxy_to_tileid(*t))
    print(f"Extracting {name} (zoom {min_zoom}-{max_zoom}, bbox {bbox})")
    print(f"  source: {source}")
    print(f"  {len(tiles)} tiles to fetch")

    map_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = map_file.with_suffix(".pmtiles.part")
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

        src_header = reader.header()
        min_lon, min_lat, max_lon, max_lat = bbox
        header = {
            "tile_type": src_header["tile_type"],
            "tile_compression": src_header["tile_compression"],
            "min_lon_e7": int(min_lon * 1e7),
            "min_lat_e7": int(min_lat * 1e7),
            "max_lon_e7": int(max_lon * 1e7),
            "max_lat_e7": int(max_lat * 1e7),
            "center_zoom": min(min_zoom + 3, max_zoom),
            "center_lon_e7": int((min_lon + max_lon) / 2 * 1e7),
            "center_lat_e7": int((min_lat + max_lat) / 2 * 1e7),
        }
        writer.finalize(header, reader.metadata())

    tmp_file.replace(map_file)
    info_file.write_text(json.dumps({
        "name": name,
        "source": source,
        "bbox": list(bbox),
        "center": [(min_lat + max_lat) / 2, (min_lon + max_lon) / 2],
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        **({"trail": trail} if trail else {}),
    }, indent=2))
    print(f"Saved {map_file} ({map_file.stat().st_size / 1e6:.1f} MB) and {info_file.name}")


def slug(name):
    """A map id (see maps.py) made from a name."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64].strip("-") or "map"


def download_map(args):
    """Download the offline basemap into data/maps/<name>.pmtiles."""
    bbox, name = resolve_area(args)
    reader, source = open_source(args.source)
    max_zoom = min(args.max_zoom, reader.header()["max_zoom"])
    tiles_file, info_file = map_files(MAPS_DIR, slug(name))
    extract_map(reader, source, tiles_in_bbox(bbox, args.min_zoom, max_zoom), bbox, name,
                args.min_zoom, max_zoom, tiles_file, info_file)
    print("Start the app with `python main.py`.")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Download offline data for the user app")
    sub = parser.add_subparsers(dest="command", required=True)

    mp = sub.add_parser("map", help="download the offline basemap")
    add_area_arguments(mp)
    mp.add_argument("--min-zoom", type=int, default=8)
    mp.add_argument("--max-zoom", type=int, default=15,
                    help="highest zoom to download (Protomaps data stops at 15; "
                         "the map still zooms further by scaling up)")
    mp.add_argument("--source",
                    help="Protomaps .pmtiles URL or local file (default: latest daily build)")
    mp.set_defaults(func=download_map)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
