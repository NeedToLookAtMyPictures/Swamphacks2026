"""JSON API over the local database.

Device (this user, never shared)
    GET    /api/device                      this device's info
    POST   /api/device                      create identity {name, user_id?, device_id?}
    PATCH  /api/device                      update mutable fields {last_server_sync}

Records created on this device
    POST   /api/locales                     {location, name, comment?}
    POST   /api/messages                    {receiver, content}
    POST   /api/messages/<origin>/<seq>/delivered   receiver marks a message delivered
    PUT    /api/location                    {latitude, longitude} this user's position

Reading
    GET    /api/<table>?since=<time>        locales, messages, geolocations, tombstones
    GET    /api/<table>/<origin>/<seq>      one locale, message or tombstone

Syncing with another device or the server
    GET    /api/sync?since=<time>           everything this device shares
    POST   /api/sync                        merge records {locales: [...], messages: [...], ...}
"""

import sqlite3

from flask import Blueprint, current_app, jsonify, request

from database import SHARED_TABLES, DeviceNotInitialized

api = Blueprint("api", __name__, url_prefix="/api")

IMMUTABLE_DEVICE_FIELDS = {"device_id", "user_id", "name", "time_created", "next_seq"}


def db():
    return current_app.config["DATABASE"]


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
    device = db().init_device(data["name"], data.get("user_id"), data.get("device_id"))
    return jsonify(device), 201


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
    if message["receiver"] != db().get_device()["user_id"]:
        return jsonify(error="only the receiver can mark a message delivered"), 403
    return jsonify(db().mark_delivered(origin_id, seq)), 201


@api.put("/location")
def set_location():
    data = body("latitude", "longitude")
    return jsonify(db().set_location(float(data["latitude"]), float(data["longitude"])))


# -- reading ------------------------------------------------------------------------

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
