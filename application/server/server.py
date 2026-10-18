"""Sync server.

Every device that can reach it sends what it has and gets back what other devices
sent. It stores the same shared tables as the user app (using its database.py)
plus a users table that keeps names unique, and the master copy of the global
data (water sources, campsites, trails; using its global_database.py) and of
the offline maps (map/<id>.pmtiles and map/<id>.json, one pair per map; see
user_app/maps.py), which devices get during sync whenever their copy differs.

Usage:
    gunicorn server:app                  # production (settings in gunicorn.conf.py)
    python server.py                     # development, http://127.0.0.1:8000
    python server.py --host 0.0.0.0      # development, reachable (and discoverable) on the wifi
    python server.py --load-global FILE  # replace the global data (devices get it on next sync)
    python download_map.py --trail ID        # download map tiles around a trail, for map/

Open the server's address in a browser to see everything in its database.

API
    GET    /api/health                   is the server up, and its server_id
    POST   /api/register                 reserve a name {name, device_id?} -> {name, device_id}
    POST   /api/sync                     {device_id, name, cursor, records, global_version?,
                                          maps? {id: version}}
                                         -> {cursor, records, merged, server_id, global_version,
                                             global (only when the device's global_version differs),
                                             maps {id: {version, size, info}} (every map, when the
                                             device sent maps; it downloads the ones it lacks)}
    GET    /api/global                   all global data {version, locales, trails (GPX)}
    GET    /api/global_locales           global locales, for the database page
    GET    /api/global_trails            global trails without their GPX, for the database page
    GET    /api/maps                     every offline map's info {id: {name, bbox, ..., version}}
    GET    /api/maps/<id>.pmtiles        one offline map (devices download it after sync)
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

from flask import Flask, jsonify, render_template, request, send_file

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "user_app"))  # share database.py with the user app

import discovery  # noqa: E402
from database import SHARED_TABLES, Database, check_name  # noqa: E402
from global_database import GlobalDatabase, read_load_file  # noqa: E402
from maps import file_version, map_files, read_maps  # noqa: E402

DB_PATH = Path(os.environ.get("SERVER_DB", HERE / "data" / "server.db"))
# The public URL is https://heuslerene.com/server, and the tunnel passes the path
# on unchanged. Requests work with or without it, so the wifi address still works.
URL_PREFIX = os.environ.get("URL_PREFIX", "/server").rstrip("/")
# Loaded into an empty global database on first start: the sample water sources
# and campsites, and the trails' GPX files in global/trails/
GLOBAL_SEED = Path(os.environ.get("GLOBAL_SEED", HERE / "global" / "global_data.json"))
# The offline maps devices get: <id>.pmtiles (Protomaps vector tiles) and <id>.json
# (its name, bounds, zoom levels and trail, as written by download_map.py)
MAP_DIR = Path(os.environ.get("MAP_DIR", HERE / "map"))

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


def read_server_maps(map_dir):
    """The maps in map_dir as {id: {path, version, size, info}}.

    A map's version is its file's SHA-256, so devices can check what they downloaded.
    """
    maps = {}
    for map_id, info in read_maps(map_dir).items():
        tiles, _ = map_files(map_dir, map_id)
        info.pop("version", None)  # the file decides the version, not what the info says
        maps[map_id] = {"path": tiles, "version": file_version(tiles),
                        "size": tiles.stat().st_size, "info": info}
    return maps


class Server:
    def __init__(self, path=DB_PATH, global_seed=GLOBAL_SEED, map_dir=MAP_DIR):
        # relay: the server has no identity and never marks messages delivered
        self.db = Database(path, relay=True)
        self.global_db = GlobalDatabase(Path(path).with_name("global.db"))
        if self.global_db.info()["version"] is None and global_seed and global_seed.exists():
            self.global_db.load(read_load_file(global_seed))
        self.maps = read_server_maps(map_dir) if map_dir else {}
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

    def sync(self, name, device_id, cursor, records, global_version=None, wants_global=False,
             map_versions=None):
        """map_versions: the device's {map id: version}, or None if it doesn't take maps."""
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
        result = {"cursor": new_cursor, "records": outgoing, "merged": merged,
                  "server_id": self.server_id}
        if wants_global:
            # Send the global data only when the device's copy differs from ours
            ours = self.global_db.info()["version"]
            result["global_version"] = ours
            if ours is not None and global_version != ours:
                result["global"] = self.global_db.export()
        if map_versions is not None:
            # Only list the maps; they're too big for the sync answer, so the device
            # downloads the ones it lacks from /api/maps/<id>.pmtiles
            result["maps"] = {map_id: {k: m[k] for k in ("version", "size", "info")}
                              for map_id, m in self.maps.items()}
        return result

    def list_users(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY name")]


class NameTaken(Exception):
    pass


class StripPrefix:
    """WSGI middleware: serve /<prefix>/api/... the same as /api/... (and tell
    Flask about the prefix, so the database page's links include it)."""

    def __init__(self, app, prefix):
        self.app, self.prefix = app, prefix

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if self.prefix and (path == self.prefix or path.startswith(self.prefix + "/")):
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.app(environ, start_response)


def create_app(path=DB_PATH, prefix=URL_PREFIX, map_dir=MAP_DIR):
    app = Flask(__name__, template_folder=str(HERE / "templates"))
    server = Server(path, map_dir=map_dir)
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
                               db_path=str(server.db.path),
                               tables=("users", "global_locales", "global_trails") + SHARED_TABLES)

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
        return jsonify(server.sync(data["name"], data["device_id"], cursor, records,
                                   data.get("global_version"), "global_version" in data,
                                   data.get("maps")))

    @app.get("/api/maps")
    def maps_info():
        return jsonify({map_id: dict(m["info"], version=m["version"]) for map_id, m in server.maps.items()})

    @app.get("/api/maps/<map_id>.pmtiles")
    def map_file(map_id):
        if map_id not in server.maps:
            return jsonify(error="this server has no such map"), 404
        return send_file(server.maps[map_id]["path"], mimetype="application/octet-stream",
                         conditional=True)

    @app.get("/api/users")
    def users():
        return jsonify(server.list_users())

    @app.get("/api/global")
    def global_data():
        return jsonify(server.global_db.export())

    @app.get("/api/global_locales")
    def global_locales():
        return jsonify(server.global_db.locales())

    @app.get("/api/global_trails")
    def global_trails():
        trails = server.global_db.trails()
        for trail in trails:
            trail["points"] = sum(len(segment) for segment in trail.pop("segments"))
        return jsonify(trails)

    @app.get("/api/<table>")
    def list_table(table):
        if table not in SHARED_TABLES:
            return jsonify(error=f"unknown table {table!r}"), 404
        since = request.args.get("since")
        return jsonify(server.db.list(table, float(since) if since is not None else None))

    app.wsgi_app = StripPrefix(app.wsgi_app, prefix)
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
    parser.add_argument("--load-global", type=Path, metavar="FILE",
                        help="replace the global data with a JSON file (format: "
                             "global/global_data.json) and exit")
    args = parser.parse_args()
    if args.load_global:
        print(app.config["SERVER"].global_db.load(read_load_file(args.load_global)))
        sys.exit()
    # The debug reloader runs this twice; only the process that serves answers discovery
    if args.host not in ("127.0.0.1", "localhost") and os.environ.get("WERKZEUG_RUN_MAIN"):
        start_lan_discovery(args.port)
    app.run(host=args.host, port=args.port, debug=True)
