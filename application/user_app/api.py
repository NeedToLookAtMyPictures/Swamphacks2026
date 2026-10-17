"""JSON API over the local database.

Device (this user, never shared)
    GET    /api/device                      this device's info
    POST   /api/device                      create identity {name}; needs the server (internet or
                                            its wifi), which reserves the name, then syncs once
    GET    /api/server                      which server this device syncs with, and when it last did
    PATCH  /api/device                      update mutable fields {last_server_sync}

Records created on this device
    POST   /api/locales                     {location, name, comment?}
    POST   /api/messages                    {receiver, content}; receiver is a name, or "*" for everyone
    POST   /api/messages/<origin>/<seq>/delivered   mark delivered by hand (normally automatic)
    PUT    /api/location                    {latitude, longitude} this user's position

Reading
    GET    /api/contacts                    everyone this device knows about (to pick a receiver)
    GET    /api/<table>?since=<time>        locales, messages, geolocations, tombstones
    GET    /api/<table>/<origin>/<seq>      one locale, message or tombstone

Syncing with another device or the server
    GET    /api/sync?since=<time>           everything this device shares
    POST   /api/sync                        merge records {locales: [...], messages: [...], ...}

Global data (set by the company, loaded once from the server; users can't change it)
    GET    /api/global/locales?category=    water sources, campsites, ...
    GET    /api/global/categories           each category with its count
    GET    /api/global/trails               trails with their lines parsed from GPX
    GET    /api/global/trails/<id>.gpx      a trail's original GPX file
    GET    /api/global/info                 {loaded_at, version}
    GET    /api/distances?from=&to=         walking distance along the global trails to global
                                            locales; from a locale id or "lat,lon" (default:
                                            this user's position), to a locale id (default: all)
    POST   /api/global/load                 {locales, trails}; replaces everything (normally the
                                            server sends it during sync instead)
"""

import sqlite3

from flask import Blueprint, Response, current_app, jsonify, request

import server_sync
from database import SHARED_TABLES, DeviceNotInitialized, check_name
from trail_distance import TrailNetwork

api = Blueprint("api", __name__, url_prefix="/api")

IMMUTABLE_DEVICE_FIELDS = {"device_id", "name", "time_created", "next_seq"}


def db():
    return current_app.config["DATABASE"]


def global_db():
    return current_app.config["GLOBAL_DATABASE"]


def trail_network():
    """The global trails as a TrailNetwork, rebuilt when the global data is reloaded."""
    loaded_at = global_db().info()["loaded_at"]
    cached = current_app.config.get("TRAIL_NETWORK")
    if cached is None or cached[0] != loaded_at:
        cached = (loaded_at, TrailNetwork.from_trails(global_db().trails()))
        current_app.config["TRAIL_NETWORK"] = cached
    return cached[1]


def body(*required):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    missing = [f for f in required if data.get(f) in (None, "")]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    return data


def since_arg():
    since = request.args.get("since")
    return float(since) if since is not None else None


@api.errorhandler(DeviceNotInitialized)
def device_not_initialized(_):
    return jsonify(error="device not initialized; POST /api/device first"), 409


@api.errorhandler(ValueError)
def bad_request(exc):
    return jsonify(error=str(exc)), 400


@api.errorhandler(sqlite3.IntegrityError)
def integrity_error(exc):
    return jsonify(error=str(exc)), 400


# -- device ---------------------------------------------------------------------

@api.get("/device")
def get_device():
    device = db().get_device()
    if device is None:
        raise DeviceNotInitialized()
    return jsonify(device)


@api.post("/device")
def init_device():
    if db().get_device() is not None:
        return jsonify(error="device already initialized; its identity is immutable"), 409
    data = body("name")
    check_name(data["name"])
    # Only the server can guarantee the name is unique, so an account can only be
    # made while one can be reached: over the internet (--server) or on its wifi
    finder = current_app.config.get("SERVER")
    server_url = finder.find() if finder else None
    if server_url is None:
        return jsonify(error="can't reach the server to reserve your name; connect to "
                             "the internet or to the server's wifi and try again"), 503
    try:
        reserved = server_sync.register(server_url, data["name"], data.get("device_id"))
    except server_sync.ServerError as exc:
        return jsonify(error=f"server refused the name: {exc}"), 409
    except OSError:
        return jsonify(error="lost the server while reserving your name; try again"), 503
    db().init_device(reserved["name"], reserved["device_id"])
    # First sync right away, so the new account starts with everyone's data
    # and the server's global data (water sources, campsites, trails)
    try:
        server_sync.sync_once(db(), server_url, global_db())
    except (OSError, server_sync.ServerError, ValueError):
        pass  # the background sync will catch up
    return jsonify(db().get_device()), 201


@api.get("/server")
def server_status():
    """Which server this device syncs with, and when it last did."""
    finder = current_app.config.get("SERVER")
    device = db().get_device()
    current = finder.current if finder else None
    return jsonify(looking_for=finder.describe() if finder else "nothing (offline only)",
                   url=current[0] if current else None,
                   via=current[1] if current else None,
                   last_server_sync=device["last_server_sync"] if device else None)


@api.patch("/device")
def update_device():
    data = body()
    blocked = IMMUTABLE_DEVICE_FIELDS & data.keys()
    if blocked:
        raise ValueError(f"cannot change {', '.join(sorted(blocked))}")
    return jsonify(db().update_device(last_server_sync=data.get("last_server_sync")))


# -- local records ----------------------------------------------------------------

@api.post("/locales")
def add_locale():
    data = body("location", "name")
    return jsonify(db().add_locale(data["location"], data["name"], data.get("comment", ""))), 201


@api.post("/messages")
def add_message():
    data = body("receiver", "content")
    return jsonify(db().add_message(data["receiver"], data["content"])), 201


@api.post("/messages/<origin_id>/<int:seq>/delivered")
def mark_delivered(origin_id, seq):
    message = db().get("messages", origin_id, seq)
    if message is None:
        return jsonify(error="no such message"), 404
    if message["receiver"].lower() != db().get_device()["name"].lower():
        return jsonify(error="only the receiver can mark a message delivered"), 403
    if message["is_dead"]:
        return jsonify(error="message is already delivered or expired"), 409
    return jsonify(db().mark_delivered(origin_id, seq)), 201


@api.put("/location")
def set_location():
    data = body("latitude", "longitude")
    return jsonify(db().set_location(float(data["latitude"]), float(data["longitude"])))


# -- reading ------------------------------------------------------------------------

@api.get("/contacts")
def contacts():
    return jsonify(db().contacts())


@api.get("/<table>")
def list_table(table):
    if table not in SHARED_TABLES:
        return jsonify(error=f"unknown table {table!r}"), 404
    return jsonify(db().list(table, since_arg()))


@api.get("/<table>/<origin_id>/<int:seq>")
def get_record(table, origin_id, seq):
    if table not in ("locales", "messages", "tombstones"):
        return jsonify(error=f"unknown table {table!r}"), 404
    record = db().get(table, origin_id, seq)
    if record is None:
        return jsonify(error="not found"), 404
    return jsonify(record)


# -- sync -----------------------------------------------------------------------------

@api.get("/sync")
def export():
    return jsonify(db().export(since_arg()))


@api.post("/sync")
def merge():
    data = body()
    unknown = data.keys() - set(SHARED_TABLES)
    if unknown:
        raise ValueError(f"unknown tables: {', '.join(sorted(unknown))}")
    return jsonify(merged=db().merge(data))


# -- global data -------------------------------------------------------------------------

@api.get("/global/locales")
def global_locales():
    return jsonify(global_db().locales(request.args.get("category")))


@api.get("/global/categories")
def global_categories():
    return jsonify(global_db().categories())


@api.get("/global/trails")
def global_trails():
    return jsonify(global_db().trails())


@api.get("/global/trails/<trail_id>.gpx")
def global_trail_gpx(trail_id):
    gpx = global_db().trail_gpx(trail_id)
    if gpx is None:
        return jsonify(error="no such trail"), 404
    return Response(gpx, mimetype="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="{trail_id}.gpx"'})


@api.get("/global/info")
def global_info():
    return jsonify(global_db().info())


@api.post("/global/load")
def global_load():
    return jsonify(global_db().load(body("locales")))


@api.get("/distances")
def distances():
    """Distance to global locales: to the nearest trail, along the trails, then to the locale.

    Nearest first; locales no trail connects to come last, by straight-line distance.
    """
    locales = global_db().locales()
    by_id = {l["id"]: l for l in locales}

    start = request.args.get("from")
    if start is None:
        me = db().get_device()
        if me is None:
            raise DeviceNotInitialized()
        position = db().get_location(me["name"])
        if position is None:
            return jsonify(error="no position yet; PUT /api/location or pass ?from="), 409
        origin = (position["latitude"], position["longitude"])
    elif start in by_id:
        origin = (by_id[start]["latitude"], by_id[start]["longitude"])
    else:
        try:
            origin = tuple(float(part) for part in start.split(","))
        except ValueError:
            origin = ()
        if len(origin) != 2:
            raise ValueError('from must be a global locale id or "lat,lon"')

    if "to" in request.args:
        if request.args["to"] not in by_id:
            return jsonify(error="no such global locale"), 404
        locales = [by_id[request.args["to"]]]

    network = trail_network()
    routes = network.distances(origin, [(l["latitude"], l["longitude"]) for l in locales])
    results = [{**locale, **route} for locale, route in zip(locales, routes)]
    results.sort(key=lambda r: (r["distance"] is None, r["distance"] or r["straight"]))
    return jsonify({"from": origin, "trails": network.bounds is not None, "locales": results})
