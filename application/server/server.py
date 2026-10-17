"""Sync server.

Every device that can reach it sends what it has and gets back what other devices
sent. It stores the same shared tables as the user app (using its database.py)
plus a users table that keeps names unique.

Usage:
    gunicorn server:app                  # production (settings in gunicorn.conf.py)
    python server.py                     # development, http://127.0.0.1:8000
    python server.py --host 0.0.0.0      # development, reachable (and discoverable) on the wifi

Open the server's address in a browser to see everything in its database.

API
    GET    /api/health                   is the server up, and its server_id
    POST   /api/register                 reserve a name {name, device_id?} -> {name, device_id}
    POST   /api/sync                     {device_id, name, cursor, records} -> {cursor, records, merged}
    GET    /api/users                    every registered name
    GET    /api/<table>?since=<time>     locales, messages, geolocations, tombstones
"""

import argparse
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "user_app"))  # share database.py with the user app

import discovery  # noqa: E402
from database import SHARED_TABLES, Database, check_name  # noqa: E402

DB_PATH = Path(os.environ.get("SERVER_DB", HERE / "data" / "server.db"))

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name          TEXT PRIMARY KEY COLLATE NOCASE,
    device_id     TEXT NOT NULL,
    time_created  REAL NOT NULL,
    last_sync     REAL
);

-- Exactly one row. Devices keep sync cursors per server_id, so a different server
-- (or a fresh database) means a full exchange instead of missing records.
CREATE TABLE IF NOT EXISTS server_info (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    server_id     TEXT NOT NULL,
    time_created  REAL NOT NULL
);
"""


class Server:
    def __init__(self, path=DB_PATH):
        # relay: the server has no identity and never marks messages delivered
        self.db = Database(path, relay=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
            conn.executescript(USERS_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO server_info VALUES (1, ?, ?)",
                         (secrets.token_hex(4), time.time()))
            self.server_id = conn.execute("SELECT server_id FROM server_info").fetchone()[0]

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
        return {"cursor": new_cursor, "records": outgoing, "merged": merged,
                "server_id": self.server_id}

    def list_users(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY name")]


class NameTaken(Exception):
    pass


def create_app(path=DB_PATH):
    app = Flask(__name__, template_folder=str(HERE / "templates"))
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

    @app.get("/")
    def dashboard():
        """Everything in the server's database, for whoever runs the server."""
        return render_template("dashboard.html", server_id=server.server_id,
                               db_path=str(server.db.path), tables=("users",) + SHARED_TABLES)

    @app.get("/api/health")
    def health():
        return jsonify(ok=True, time=time.time(), server_id=server.server_id)

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


def start_lan_discovery(http_port):
    """Answer devices looking for a server on the wifi (see user_app/discovery.py)."""
    if os.environ.get("LAN_DISCOVERY", "1") == "0":
        return
    try:
        discovery.start_responder(http_port)
        print(f"Devices on the same wifi can find this server "
              f"(UDP port {discovery.DISCOVERY_PORT})")
    except OSError as exc:
        print(f"Wifi discovery is off: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync server (development)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="0.0.0.0 to be reachable, and discoverable, on the wifi")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    # The debug reloader runs this twice; only the process that serves answers discovery
    if args.host not in ("127.0.0.1", "localhost") and os.environ.get("WERKZEUG_RUN_MAIN"):
        start_lan_discovery(args.port)
    app.run(host=args.host, port=args.port, debug=True)
