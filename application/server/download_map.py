"""Download the offline map tiles for a trail, ready to put on the server.

Run this while you have internet:
    python download_map.py --trail trail-john-muir     # one of the server's trails, by id
    python download_map.py --gpx some_trail.gpx
    python download_map.py --place "Gainesville to Hawthorne, Florida"
    python download_map.py --bbox -82.42 29.56 -82.05 29.73 --name "Gainesville to Hawthorne"

It writes map.pmtiles and map.json into downloads/<name>/ (git-ignored), so you
can look at the size before using it. To put it on the server, copy both into
map/ and restart the server (or pass --out map to write there directly):
    cp downloads/john-muir-trail/map.* map/

--trail reads the trail's GPX from the server's global data (data/global.db, or
global/global_data.json if the server hasn't started yet). With --trail or
--gpx, the map covers the trail's bounds plus --margin-km. Up to
--full-zoom the whole area is downloaded, for context when zoomed out; above it
only the tiles within --corridor-km of the trail, which keeps a long trail's
map small. The tiles come from the latest daily Protomaps world build
(OpenStreetMap data), via user_app/download_locales.py.
"""

import argparse
import math
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "user_app"))  # share the map code with the user app

from download_locales import (  # noqa: E402
    add_area_arguments, extract_map, lonlat_to_tile, open_source, resolve_area, tiles_in_bbox)
from global_database import GlobalDatabase, parse_gpx, read_load_file  # noqa: E402

DOWNLOADS_DIR = HERE / "downloads"
# Same places server.py uses
GLOBAL_DB = Path(os.environ.get("SERVER_DB", HERE / "data" / "server.db")).with_name("global.db")
GLOBAL_SEED = Path(os.environ.get("GLOBAL_SEED", HERE / "global" / "global_data.json"))
KM_PER_DEGREE = 111.32  # of latitude, and of longitude at the equator


def server_trail(trail_id):
    """(name, GPX) of one of the server's trails: from its database, else from the seed file."""
    if GLOBAL_DB.exists():
        db = GlobalDatabase(GLOBAL_DB)
        gpx = db.trail_gpx(trail_id)
        if gpx is not None:
            return next(t["name"] for t in db.trails() if t["id"] == trail_id), gpx
        trails = db.trails()
    else:
        trails = read_load_file(GLOBAL_SEED)["trails"]
        for trail in trails:
            if trail["id"] == trail_id:
                return trail["name"], trail["gpx"]
    ids = ", ".join(t["id"] for t in trails) or "none"
    raise SystemExit(f"The server has no trail {trail_id!r}. Its trails: {ids}")


def gpx_name(gpx):
    match = re.search(r"<name>\s*(.*?)\s*</name>", gpx, re.S)
    return match.group(1) if match else None


def padded_bbox(points, margin_km):
    """(min_lon, min_lat, max_lon, max_lat) around points ([lat, lon]), margin_km wider each way."""
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    dlat = margin_km / KM_PER_DEGREE
    dlon = margin_km / (KM_PER_DEGREE * math.cos(math.radians((min(lats) + max(lats)) / 2)))
    return (round(min(lons) - dlon, 4), round(min(lats) - dlat, 4),
            round(max(lons) + dlon, 4), round(max(lats) + dlat, 4))


def corridor_tiles(points, zoom, corridor_km):
    """The tiles at zoom within about corridor_km of any of points ([lat, lon])."""
    n = 2 ** zoom
    centers = {lonlat_to_tile(lon, lat, zoom) for lat, lon in points}
    lat = sum(p[0] for p in points) / len(points)
    tile_km = 2 * math.pi * 6371 * math.cos(math.radians(lat)) / n
    k = math.ceil(corridor_km / tile_km)
    return {(zoom, x, y)
            for cx, cy in centers
            for x in range(max(cx - k, 0), min(cx + k, n - 1) + 1)
            for y in range(max(cy - k, 0), min(cy + k, n - 1) + 1)}


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "map"


def main():
    parser = argparse.ArgumentParser(description="Download offline map tiles for the server")
    area = parser.add_mutually_exclusive_group()
    area.add_argument("--trail", metavar="ID", help="one of the server's trails, e.g. trail-john-muir")
    area.add_argument("--gpx", type=Path, help="a trail's GPX file")
    add_area_arguments(parser)
    parser.add_argument("--name", help="the map's name (default: the trail's name, or the place)")
    parser.add_argument("--margin-km", type=float, default=5,
                        help="with --trail/--gpx, how far past the trail's ends and sides the map reaches (default 5)")
    parser.add_argument("--corridor-km", type=float, default=3,
                        help="with --trail/--gpx, above --full-zoom only tiles this close to the trail (default 3)")
    parser.add_argument("--full-zoom", type=int, default=11,
                        help="with --trail/--gpx, highest zoom downloaded for the whole area (default 11)")
    parser.add_argument("--min-zoom", type=int, default=8)
    parser.add_argument("--max-zoom", type=int, default=15,
                        help="highest zoom to download (Protomaps data stops at 15; "
                             "the map still zooms further by scaling up)")
    parser.add_argument("--source",
                        help="Protomaps .pmtiles URL or local file (default: latest daily build)")
    parser.add_argument("--out", type=Path,
                        help="folder for map.pmtiles and map.json (default: downloads/<name>/; "
                             "map to replace the server's map)")
    args = parser.parse_args()

    reader, source = open_source(args.source)
    max_zoom = min(args.max_zoom, reader.header()["max_zoom"])

    if args.trail or args.gpx:
        if args.trail:
            trail_name, gpx = server_trail(args.trail)
        else:
            gpx = args.gpx.read_text()
            trail_name = gpx_name(gpx) or args.gpx.stem
        points = [p for segment in parse_gpx(gpx) for p in segment]
        name = args.name or trail_name
        bbox = padded_bbox(points, args.margin_km)
        full_zoom = min(args.full_zoom, max_zoom)
        tiles = set(tiles_in_bbox(bbox, args.min_zoom, full_zoom))
        for zoom in range(max(full_zoom + 1, args.min_zoom), max_zoom + 1):
            tiles |= corridor_tiles(points, zoom, args.corridor_km)
    else:
        bbox, name = resolve_area(args)
        name = args.name or name
        tiles = set(tiles_in_bbox(bbox, args.min_zoom, max_zoom))

    out = args.out or DOWNLOADS_DIR / slug(name)
    extract_map(reader, source, tiles, bbox, name, args.min_zoom, max_zoom,
                out / "map.pmtiles", out / "map.json")
    if out.resolve() == (HERE / "map").resolve():
        print("Restart the server; devices get the new map on their next sync.")
    else:
        print(f"To put it on the server: cp {out}/map.* map/   then restart the server.")


if __name__ == "__main__":
    main()
