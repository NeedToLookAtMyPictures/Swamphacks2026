"""Download the offline map tiles for a trail, ready to put on the server.

Run this while you have internet:
    python download_map.py --trail trail-john-muir-yosemite   # one of the server's trails, by id
    python download_map.py --gpx some_trail.gpx
    python download_map.py --place "Gainesville to Hawthorne, Florida"
    python download_map.py --bbox -82.42 29.56 -82.05 29.73 --name "Gainesville to Hawthorne"

It writes <id>.pmtiles and <id>.json into downloads/ (git-ignored), so you can
look at the size before using it. The id is the trail's id without "trail-", or
the name. To put it on the server, copy both into map/ next to the other maps
and restart the server (or pass --out map to write there directly):
    cp downloads/john-muir-yosemite.* map/
The server sends every map in map/ to devices; with --trail, the app shows this
map when that trail is selected in Explore.

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
    add_area_arguments, extract_map, lonlat_to_tile, open_source, resolve_area, slug, tiles_in_bbox)
from global_database import GlobalDatabase, parse_gpx, read_load_file  # noqa: E402
from maps import map_files  # noqa: E402

DOWNLOADS_DIR = HERE / "downloads"
# Same places server.py uses
GLOBAL_DB = Path(os.environ.get("SERVER_DB", HERE / "data" / "server.db")).with_name("global.db")
GLOBAL_SEED = Path(os.environ.get("GLOBAL_SEED", HERE / "global" / "global_data.json"))
KM_PER_DEGREE = 111.32  # of latitude, and of longitude at the equator


def server_trail(trail_id):
    """(name, GPX) of one of the server's trails: from its database, else from the seed file."""
    db_trails = []
    if GLOBAL_DB.exists():
        db = GlobalDatabase(GLOBAL_DB)
        gpx = db.trail_gpx(trail_id)
        if gpx is not None:
            return next(t["name"] for t in db.trails() if t["id"] == trail_id), gpx
        db_trails = db.trails()
    # A server started before the trail was added to the seed file doesn't have it yet
    seed_trails = read_load_file(GLOBAL_SEED)["trails"]
    for trail in seed_trails:
        if trail["id"] == trail_id:
            if GLOBAL_DB.exists():
                print(f"Using {trail_id} from {GLOBAL_SEED.relative_to(HERE)}; the server doesn't have it yet.\n"
                      f"Give it to the server: python server.py --load-global {GLOBAL_SEED.relative_to(HERE)}")
            return trail["name"], trail["gpx"]
    ids = ", ".join(sorted({t["id"] for t in db_trails + seed_trails})) or "none"
    raise SystemExit(f"No trail {trail_id!r} on the server or in {GLOBAL_SEED.relative_to(HERE)}. Trails: {ids}")


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


def main():
    parser = argparse.ArgumentParser(description="Download offline map tiles for the server")
    area = parser.add_mutually_exclusive_group()
    area.add_argument("--trail", metavar="ID", help="one of the server's trails, e.g. trail-john-muir-yosemite")
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
    parser.add_argument("--id", help="the map's id, its file name (default: the trail id without "
                                     "\"trail-\", or from the name)")
    parser.add_argument("--out", type=Path, default=DOWNLOADS_DIR,
                        help="folder for <id>.pmtiles and <id>.json (default: downloads; "
                             "map to add it to the server's maps)")
    args = parser.parse_args()

    # Find the trail first, so a wrong id fails before connecting to anything
    if args.trail:
        trail_name, gpx = server_trail(args.trail)
    elif args.gpx:
        gpx = args.gpx.read_text()
        trail_name = gpx_name(gpx) or args.gpx.stem

    reader, source = open_source(args.source)
    max_zoom = min(args.max_zoom, reader.header()["max_zoom"])

    if args.trail or args.gpx:
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

    map_id = args.id or (args.trail.removeprefix("trail-") if args.trail else slug(name))
    tiles_file, info_file = map_files(args.out, map_id)
    extract_map(reader, source, tiles, bbox, name, args.min_zoom, max_zoom, tiles_file, info_file,
                trail=args.trail)
    if args.out.resolve() == (HERE / "map").resolve():
        print("Restart the server; devices get the new map on their next sync.")
    else:
        print(f"To put it on the server: cp {args.out}/{map_id}.* map/   then restart the server.")


if __name__ == "__main__":
    main()
