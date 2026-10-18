"""Global SQLite database: data set by the company and loaded from the server.

Separate from the user's own database (database.py). The server keeps the
master copy (server/server.py uses this same module) and sends it to devices
when they sync and their copy is out of date; users never change it. The only
writer is load(), which replaces everything.

Global locales are fixed places such as water sources and campsites. Their `id`
is what user locale records (reviews/comments) refer to in their `location`.
Global trails are routes, sent as GPX, always drawn on the map.

Usage (load a JSON file in the format the server sends, e.g. for testing offline):
    python global_database.py load ../server/global/global_data.json
"""

import argparse
import contextlib
import hashlib
import json
import math
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_GLOBAL_DB_PATH = Path(__file__).resolve().parent / "data" / "global.db"
GLOBAL_SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS global_locales (
    id          TEXT PRIMARY KEY,  -- set by the server; user locales refer to it
    category    TEXT NOT NULL,     -- e.g. water, campsite
    name        TEXT NOT NULL,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS global_trails (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    gpx         TEXT NOT NULL      -- the GPX document as sent by the server
);

-- Exactly one row: when the global data was loaded, and which data it is
CREATE TABLE IF NOT EXISTS global_info (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    loaded_at REAL,  -- NULL until loaded from the server
    version   TEXT   -- hash of the content: equal on server and device when in sync
);
INSERT OR IGNORE INTO global_info (id) VALUES (1);
"""

LOCALE_COLUMNS = ("id", "category", "name", "latitude", "longitude", "description")
TRAIL_COLUMNS = ("id", "name", "description", "gpx")


def parse_gpx(gpx):
    """The lines in a GPX document: a list of segments, each a list of [lat, lon].

    Reads track segments (trkseg/trkpt) and routes (rte/rtept); namespaces are ignored.
    """
    try:
        root = ET.fromstring(gpx)
    except ET.ParseError as exc:
        raise ValueError(f"invalid GPX: {exc}") from None
    local = lambda el: el.tag.rsplit("}", 1)[-1]
    segments = []
    for el in root.iter():
        point_tag = {"trkseg": "trkpt", "rte": "rtept"}.get(local(el))
        if point_tag:
            points = [[float(p.get("lat")), float(p.get("lon"))]
                      for p in el if local(p) == point_tag]
            if len(points) >= 2:
                segments.append(points)
    if not segments:
        raise ValueError("GPX has no track or route with at least 2 points")
    return segments


def length_km(segments):
    rad = math.pi / 180
    total = 0.0
    for points in segments:
        for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
            a = (math.sin((lat2 - lat1) * rad / 2) ** 2 + math.cos(lat1 * rad)
                 * math.cos(lat2 * rad) * math.sin((lon2 - lon1) * rad / 2) ** 2)
            total += 12742 * math.asin(math.sqrt(a))
    return total


class GlobalDatabase:
    def __init__(self, path=DEFAULT_GLOBAL_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        version = self._version()
        if version not in (0, GLOBAL_SCHEMA_VERSION):
            # Only a copy of server data, so an old file can just be replaced
            self.path.unlink()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {GLOBAL_SCHEMA_VERSION}")

    def _version(self):
        with self._connect() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    @contextlib.contextmanager
    def _connect(self):
        """A connection that commits on success, rolls back on error, and closes."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- reading ----------------------------------------------------------------

    def locales(self, category=None):
        query, params = "SELECT * FROM global_locales", ()
        if category is not None:
            query, params = query + " WHERE category = ?", (category,)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query + " ORDER BY category, name", params)]

    def categories(self):
        """Each category with how many locales it has."""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT category, COUNT(*) AS count FROM global_locales "
                "GROUP BY category ORDER BY category")]

    def trails(self):
        """Each trail with its line parsed from the GPX: segments of [lat, lon] and length."""
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM global_trails ORDER BY name")]
        for row in rows:
            row["segments"] = parse_gpx(row.pop("gpx"))
            row["length_km"] = round(length_km(row["segments"]), 2)
        return rows

    def trail_gpx(self, trail_id):
        with self._connect() as conn:
            row = conn.execute("SELECT gpx FROM global_trails WHERE id = ?", (trail_id,)).fetchone()
        return row["gpx"] if row else None

    def info(self):
        with self._connect() as conn:
            return dict(conn.execute("SELECT loaded_at, version FROM global_info").fetchone())

    def export(self):
        """Everything, in the format load() takes (trails as GPX text), plus its version."""
        with self._connect() as conn:
            locales = [dict(r) for r in conn.execute("SELECT * FROM global_locales ORDER BY id")]
            trails = [dict(r) for r in conn.execute("SELECT * FROM global_trails ORDER BY id")]
        return {"version": self.info()["version"], "locales": locales, "trails": trails}

    # -- loading from the server --------------------------------------------------

    def load(self, data):
        """Replace all global data with what the server sends.

        data = {
            "locales": [{id, category, name, latitude, longitude, description?}, ...],
            "trails": [{id, name, description?, gpx: "<gpx>...</gpx>"}, ...],  (optional)
        }
        Returns how many of each were loaded. Nothing changes if any record is bad.
        """
        if not isinstance(data, dict) or not isinstance(data.get("locales"), list) \
                or not isinstance(data.get("trails", []), list):
            raise ValueError("expected {locales: [...], trails: [...]}")
        locales = [self._clean(r) for r in data["locales"]]
        trails = [self._clean_trail(r) for r in data.get("trails", [])]
        version = content_version(locales, trails)
        with self._connect() as conn:
            for table, columns, rows in (("global_locales", LOCALE_COLUMNS, locales),
                                         ("global_trails", TRAIL_COLUMNS, trails)):
                conn.execute(f"DELETE FROM {table}")
                conn.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join(':' + c for c in columns)})", rows)
            conn.execute("UPDATE global_info SET loaded_at = ?, version = ?",
                         (time.time(), version))
        return {"locales": len(locales), "trails": len(trails)}

    # -- helpers -------------------------------------------------------------------

    @staticmethod
    def _clean(record):
        if not isinstance(record, dict):
            raise ValueError("each global locale must be an object")
        missing = [f for f in ("id", "category", "name", "latitude", "longitude")
                   if record.get(f) in (None, "")]
        if missing:
            raise ValueError(f"global locale is missing {', '.join(missing)}")
        return {
            "id": str(record["id"]),
            "category": str(record["category"]).strip().lower(),
            "name": str(record["name"]),
            "latitude": float(record["latitude"]),
            "longitude": float(record["longitude"]),
            "description": str(record.get("description") or ""),
        }


    @staticmethod
    def _clean_trail(record):
        if not isinstance(record, dict):
            raise ValueError("each global trail must be an object")
        missing = [f for f in ("id", "name", "gpx") if record.get(f) in (None, "")]
        if missing:
            raise ValueError(f"global trail is missing {', '.join(missing)}")
        parse_gpx(record["gpx"])  # refuse GPX we can't draw
        return {
            "id": str(record["id"]),
            "name": str(record["name"]),
            "description": str(record.get("description") or ""),
            "gpx": record["gpx"],
        }


def content_version(locales, trails):
    """A short hash of cleaned global data: the same data always gets the same
    version, so a device can tell whether its copy matches the server's."""
    data = {"locales": sorted(locales, key=lambda r: r["id"]),
            "trails": sorted(trails, key=lambda r: r["id"])}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def read_load_file(path):
    """Read a JSON file in the server's format. In a file, a trail may give
    "gpx_file" (relative to the JSON file) instead of the GPX text."""
    data = json.loads(path.read_text())
    for trail in data.get("trails", []):
        if "gpx_file" in trail:
            trail["gpx"] = (path.parent / trail.pop("gpx_file")).read_text()
    return data


def main():
    parser = argparse.ArgumentParser(description="Global (server-owned) database")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="replace the global data with a JSON file in the server's format")
    load.add_argument("file", type=Path)
    args = parser.parse_args()
    print(GlobalDatabase().load(read_load_file(args.file)))


if __name__ == "__main__":
    main()
