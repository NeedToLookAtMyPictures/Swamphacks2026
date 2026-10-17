"""Sync server.

Every device that can reach it sends what it has and gets back what other devices
sent. It stores the same shared tables as the user app (using its database.py)
plus a users table that keeps names unique.

Usage:
    gunicorn server:app                  # production (settings in gunicorn.conf.py)
    python server.py                     # development, http://127.0.0.1:8000

API
    GET    /api/health                   is the server up
    POST   /api/register                 reserve a name {name, device_id?} -> {name, device_id}
    POST   /api/sync                     {device_id, name, cursor, records} -> {cursor, records, merged}
    GET    /api/users                    every registered name
    GET    /api/<table>?since=<time>     locales, messages, geolocations, tombstones
"""

import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "user_app"))  # share database.py with the user app

from database import SHARED_TABLES, Database, check_name  # noqa: E402

DB_PATH = Path(os.environ.get("SERVER_DB", HERE / "data" / "server.db"))

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name          TEXT PRIMARY KEY COLLATE NOCASE,
    device_id     TEXT NOT NULL,
    time_created  REAL NOT NULL,
    last_sync     REAL
);
"""


class Server:
    def __init__(self, path=DB_PATH):
        # relay: the server has no identity and never marks messages delivered
        self.db = Database(path, relay=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
            conn.executescript(USERS_SCHEMA)

    def connect(self):
        # The users table lives in the same file, next to the shared tables
        return self.db._connect()

    def register(self, name, device_id=None):
        """Reserve a name for a device. Registering again from the same device is fine."""
        check_name(name)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
            if row is not None:
                if device_id is not None and row["device_id"] == device_id:
                    return {"name": row["name"], "device_id": row["device_id"]}
                raise NameTaken(name)
            device_id = device_id or secrets.token_hex(4)
            conn.execute("INSERT INTO users (name, device_id, time_created) VALUES (?, ?, ?)",
                         (name, device_id, time.time()))
        return {"name": name, "device_id": device_id}

    def sync(self, name, device_id, cursor, records):
        # Collect what the device is missing before merging what it sent,
        # so it doesn't get its own records straight back
        outgoing, new_cursor = self.db.changes(cursor)
        merged = self.db.merge(records)
        with self.connect() as conn:
            # A device created offline is registered the first time it syncs
            conn.execute("INSERT OR IGNORE INTO users (name, device_id, time_created) "
                         "VALUES (?, ?, ?)", (name, device_id, time.time()))
            conn.execute("UPDATE users SET last_sync = ? WHERE name = ? AND device_id = ?",
                         (time.time(), name, device_id))
        return {"cursor": new_cursor, "records": outgoing, "merged": merged}

    def list_users(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY name")]


class NameTaken(Exception):
    pass


def create_app(path=DB_PATH):
    app = Flask(__name__)
    server = Server(path)
    app.config["SERVER"] = server

    def body(*required):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        missing = [f for f in required if data.get(f) in (None, "")]
        if missing:
            raise ValueError(f"missing {', '.join(missing)}")
        return data

    @app.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(sqlite3.IntegrityError)
    def integrity_error(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(NameTaken)
    def name_taken(exc):
        return jsonify(error=f"the name {exc} is already taken"), 409

    @app.get("/api/health")
    def health():
        return jsonify(ok=True, time=time.time())

    @app.post("/api/register")
    def register():
        data = body("name")
        return jsonify(server.register(data["name"], data.get("device_id"))), 201

    @app.post("/api/sync")
    def sync():
        data = body("name", "device_id")
        check_name(data["name"])
        records = data.get("records") or {}
        if not isinstance(records, dict):
            raise ValueError("records must be an object")
        unknown = records.keys() - set(SHARED_TABLES)
        if unknown:
            raise ValueError(f"unknown tables: {', '.join(sorted(unknown))}")
        cursor = data.get("cursor")
        cursor = float(cursor) if cursor is not None else None
        return jsonify(server.sync(data["name"], data["device_id"], cursor, records))

    @app.get("/api/users")
    def users():
        return jsonify(server.list_users())

    @app.get("/api/<table>")
    def list_table(table):
        if table not in SHARED_TABLES:
            return jsonify(error=f"unknown table {table!r}"), 404
        since = request.args.get("since")
        return jsonify(server.db.list(table, float(since) if since is not None else None))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
