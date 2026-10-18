"""Offline maps: a folder holding any number of maps, each a pair of files.

    <id>.pmtiles   Protomaps vector tiles
    <id>.json      {name, bbox, center, min_zoom, max_zoom, trail?, version?}

`trail` is the id of the global trail the map was made for (download_map.py
--trail sets it); the app shows that map when the trail is selected. `version`
is the SHA-256 of the .pmtiles file, set on maps that came from the server.

The server keeps its maps in server/map/, the app in data/maps/. Both use this
module (the server imports it from here, like database.py).
"""

import hashlib
import json
import re
from pathlib import Path

MAP_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")  # also safe as a file name


def map_files(maps_dir, map_id):
    """(tiles, info) paths of map_id in maps_dir. ValueError for an id that isn't a plain name."""
    if not MAP_ID.fullmatch(map_id):
        raise ValueError(f"invalid map id: {map_id!r}")
    return Path(maps_dir) / f"{map_id}.pmtiles", Path(maps_dir) / f"{map_id}.json"


def read_maps(maps_dir):
    """{id: info} for every complete map in maps_dir, with "id" added to each info."""
    maps = {}
    for tiles in sorted(Path(maps_dir).glob("*.pmtiles")):
        map_id = tiles.stem
        if not MAP_ID.fullmatch(map_id):
            continue
        try:
            info = json.loads(tiles.with_suffix(".json").read_text())
        except (OSError, ValueError):
            continue  # no info file (yet): not a usable map
        maps[map_id] = dict(info, id=map_id)
    return maps


def file_version(path):
    """A map file's version: its SHA-256."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
