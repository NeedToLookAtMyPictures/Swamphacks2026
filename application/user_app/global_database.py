"""Global SQLite database: data set by the company and synced from the server.

Separate from the user's own database (database.py). Users never change it;
the only writer is sync(), which applies what the server sends.

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
GLOBAL_SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS global_locales (
    id           TEXT    PRIMARY KEY,  -- set by the server; user locales refer to it
    category     TEXT    NOT NULL,     -- e.g. water, campsite
    name         TEXT    NOT NULL,
    latitude     REAL    NOT NULL,
    longitude    REAL    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    time_updated REAL    NOT NULL      -- server time of the last change
);

-- Exactly one row: when this device last synced the global data
CREATE TABLE IF NOT EXISTS global_sync (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    server_time REAL,  -- server's clock at the last sync; send it back as `since`
    synced_at   REAL   -- this device's clock at the last sync
);
INSERT OR IGNORE INTO global_sync (id) VALUES (1);
"""

LOCALE_COLUMNS = ("id", "category", "name", "latitude", "longitude", "description",
                  "time_updated")


class GlobalDatabase:
    def __init__(self, path=DEFAULT_GLOBAL_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone()
            if has_tables and version != GLOBAL_SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} uses an old schema; delete it and restart")
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {GLOBAL_SCHEMA_VERSION}")

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

    def sync_state(self):
        with self._connect() as conn:
            row = dict(conn.execute("SELECT server_time, synced_at FROM global_sync").fetchone())
        return row

    # -- syncing from the server ------------------------------------------------

    def sync(self, data):
        """Apply global data from the server.

        data = {
            "server_time": <server clock now>,
            "full": true to replace everything (otherwise only changes since our last sync),
            "locales": [{id, category, name, latitude, longitude, description?, time_updated,
                         deleted?}, ...],
        }
        A locale with "deleted": true is removed. Returns counts of what changed.
        """
        if not isinstance(data, dict) or data.get("server_time") is None:
            raise ValueError("expected {server_time, locales, full?}")
        records = data.get("locales", [])
        if not isinstance(records, list):
            raise ValueError("locales must be a list")
        counts = {"updated": 0, "deleted": 0}
        with self._connect() as conn:
            if data.get("full"):
                counts["deleted"] += conn.execute("DELETE FROM global_locales").rowcount
            for record in records:
                if record.get("deleted"):
                    counts["deleted"] += conn.execute(
                        "DELETE FROM global_locales WHERE id = ?", (self._require(record, "id"),)
                    ).rowcount
                    continue
                cur = conn.execute(
                    f"INSERT INTO global_locales ({', '.join(LOCALE_COLUMNS)}) "
                    f"VALUES ({', '.join(':' + c for c in LOCALE_COLUMNS)}) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    + ", ".join(f"{c} = excluded.{c}" for c in LOCALE_COLUMNS[1:])
                    + " WHERE excluded.time_updated >= global_locales.time_updated",
                    self._clean(record))
                counts["updated"] += cur.rowcount
            conn.execute("UPDATE global_sync SET server_time = ?, synced_at = ?",
                         (float(data["server_time"]), time.time()))
        return counts

    # -- helpers -------------------------------------------------------------------

    @staticmethod
    def _require(record, field):
        if record.get(field) in (None, ""):
            raise ValueError(f"global locale is missing {field}")
        return record[field]

    @classmethod
    def _clean(cls, record):
        for field in ("id", "category", "name", "latitude", "longitude", "time_updated"):
            cls._require(record, field)
        return {
            "id": str(record["id"]),
            "category": str(record["category"]).strip().lower(),
            "name": str(record["name"]),
            "latitude": float(record["latitude"]),
            "longitude": float(record["longitude"]),
            "description": str(record.get("description") or ""),
            "time_updated": float(record["time_updated"]),
        }


def main():
    parser = argparse.ArgumentParser(description="Global (server-owned) database")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="replace the global data with a JSON file in the server's format")
    load.add_argument("file", type=Path)
    args = parser.parse_args()

    data = json.loads(args.file.read_text())
    data.setdefault("full", True)
    data.setdefault("server_time", time.time())
    print(GlobalDatabase().sync(data))


if __name__ == "__main__":
    main()
