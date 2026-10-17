"""Global SQLite database: data set by the company and loaded from the server.

Separate from the user's own database (database.py). It is set once, from the
server at setup; users never change it. The only writer is load(), which
replaces everything.

Global locales are fixed places such as water sources and campsites. Their `id`
is what user locale records (reviews/comments) refer to in their `location`.

Usage (until the server exists, load a JSON file in the same format the server sends):
    python global_database.py load sample_global_locales.json
"""

import argparse
import contextlib
import json
import sqlite3
import time
from pathlib import Path

DEFAULT_GLOBAL_DB_PATH = Path(__file__).resolve().parent / "data" / "global.db"
GLOBAL_SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS global_locales (
    id          TEXT PRIMARY KEY,  -- set by the server; user locales refer to it
    category    TEXT NOT NULL,     -- e.g. water, campsite
    name        TEXT NOT NULL,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

-- Exactly one row: when the global data was loaded
CREATE TABLE IF NOT EXISTS global_info (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    loaded_at REAL  -- NULL until loaded from the server
);
INSERT OR IGNORE INTO global_info (id) VALUES (1);
"""

LOCALE_COLUMNS = ("id", "category", "name", "latitude", "longitude", "description")


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

    def info(self):
        with self._connect() as conn:
            return dict(conn.execute("SELECT loaded_at FROM global_info").fetchone())

    # -- loading from the server --------------------------------------------------

    def load(self, data):
        """Replace all global data with what the server sends.

        data = {"locales": [{id, category, name, latitude, longitude, description?}, ...]}
        Returns how many locales were loaded. Nothing changes if any record is bad.
        """
        records = data.get("locales") if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise ValueError("expected {locales: [...]}")
        rows = [self._clean(r) for r in records]
        with self._connect() as conn:
            conn.execute("DELETE FROM global_locales")
            conn.executemany(
                f"INSERT INTO global_locales ({', '.join(LOCALE_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in LOCALE_COLUMNS)})", rows)
            conn.execute("UPDATE global_info SET loaded_at = ?", (time.time(),))
        return {"locales": len(rows)}

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


def main():
    parser = argparse.ArgumentParser(description="Global (server-owned) database")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="replace the global data with a JSON file in the server's format")
    load.add_argument("file", type=Path)
    args = parser.parse_args()
    print(GlobalDatabase().load(json.loads(args.file.read_text())))


if __name__ == "__main__":
    main()
