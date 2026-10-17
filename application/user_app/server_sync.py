"""Background sync with the server.

While the app is running, a thread checks every CHECK_EVERY seconds whether the
server can be reached. Whenever it can, the device sends the records that
arrived since its last push and merges what the server got since its last pull.
When the device is offline the request just fails and it tries again later.

Only the standard library is used, so the app has no extra dependency.
"""

import json
import threading
import time
import urllib.error
import urllib.request

CHECK_EVERY = 30  # seconds
TIMEOUT = 15      # seconds


class ServerError(Exception):
    """The server answered, but with an error."""


def post(server_url, path, payload):
    request = urllib.request.Request(
        server_url.rstrip("/") + path, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc).get("error", exc.reason)
        except ValueError:
            message = exc.reason
        raise ServerError(f"{exc.code}: {message}") from exc


def register(server_url, name, device_id=None):
    """The startup handshake: ask the server to reserve a name. Returns {name, device_id}."""
    return post(server_url, "/api/register", {"name": name, "device_id": device_id})


def sync_once(db, server_url):
    """One full exchange with the server. Raises OSError when it can't be reached."""
    device = db.get_device()
    if device is None:
        return None  # no identity yet, nothing to sync
    records, push_cursor = db.changes(device["last_push"])
    result = post(server_url, "/api/sync", {
        "device_id": device["device_id"],
        "name": device["name"],
        "cursor": device["server_cursor"],
        "records": records,
    })
    merged = db.merge(result["records"])
    db.update_device(last_server_sync=time.time(), last_push=push_cursor,
                     server_cursor=result["cursor"])
    return merged


def start(db, server_url):
    """Sync with the server in a background thread for as long as the app runs."""
    def loop():
        online = False
        while True:
            try:
                merged = sync_once(db, server_url)
                if not online:
                    print(f"Connected to {server_url}")
                online = True
                if merged and any(merged.values()):
                    print(f"Synced with server: {merged}")
            except (OSError, ServerError, ValueError) as exc:
                if online or isinstance(exc, ServerError):
                    print(f"Server sync failed: {exc}")
                online = False
            time.sleep(CHECK_EVERY)

    thread = threading.Thread(target=loop, name="server-sync", daemon=True)
    thread.start()
    return thread
