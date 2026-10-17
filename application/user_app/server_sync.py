"""Background sync with the server.

While the app is running, a thread looks for a server every CHECK_EVERY seconds:
the ones given with --server, or else one on the same wifi (see discovery.py).
Once it reaches one, it syncs with it every SYNC_EVERY seconds: the device sends
the records that arrived since its last push and merges what the server got
since its last pull. When the server's global data (water sources, campsites,
trails) differs from the device's copy, it comes along and replaces it. If a
sync fails, it goes back to looking.

Only the standard library is used, so the app has no extra dependency.
"""

import json
import threading
import time
import urllib.error
import urllib.request

import discovery

SYNC_EVERY = 5     # seconds between syncs while a server answers
CHECK_EVERY = 30   # seconds between looking for a server while there is none
TIMEOUT = 15       # seconds, for a sync
PING_TIMEOUT = 3   # seconds, for checking whether a server is there


class ServerError(Exception):
    """The server answered, but with an error."""


def call(server_url, path, payload=None, timeout=TIMEOUT):
    """GET path, or POST payload to it as JSON. Returns the JSON answer."""
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(server_url.rstrip("/") + path, data=data,
                                     # free ngrok tunnels show browsers a warning page first
                                     headers={"content-type": "application/json",
                                              "ngrok-skip-browser-warning": "1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc).get("error", exc.reason)
        except ValueError:
            message = exc.reason
        raise ServerError(f"{exc.code}: {message}") from exc


def register(server_url, name, device_id=None):
    """The startup handshake: ask the server to reserve a name. Returns {name, device_id}."""
    return call(server_url, "/api/register", {"name": name, "device_id": device_id})


def sync_once(db, server_url, global_db=None):
    """One full exchange with the server. Raises OSError when it can't be reached.

    With global_db (a GlobalDatabase), also gets the server's global data when
    this device's copy is missing or out of date."""
    device = db.get_device()
    if device is None:
        return None  # no identity yet, nothing to sync
    server_id = call(server_url, "/api/health", timeout=PING_TIMEOUT).get("server_id")
    last_push, server_cursor = device["last_push"], device["server_cursor"]
    if server_id != device["server_id"]:
        # The cursors belong to another server (e.g. the internet one, and now one
        # on the wifi): this one may be missing anything, so exchange everything
        last_push = server_cursor = None
    records, push_cursor = db.changes(last_push)
    request = {
        "device_id": device["device_id"],
        "name": device["name"],
        "cursor": server_cursor,
        "records": records,
    }
    if global_db is not None:
        request["global_version"] = global_db.info()["version"]
    result = call(server_url, "/api/sync", request)
    merged = db.merge(result["records"])
    if global_db is not None and result.get("global"):
        loaded = global_db.load(result["global"])
        print(f"Got the server's global data: {loaded}")
    db.update_device(last_server_sync=time.time(), last_push=push_cursor,
                     server_cursor=result["cursor"], server_id=server_id)
    return merged


class ServerFinder:
    """Which server to talk to right now: the first configured one that answers,
    else one found on the wifi. find() returns its URL, or None when there is none.

    urls is a list, or one string of comma-separated URLs (e.g. the public URL
    first, then the server's wifi address).
    """

    def __init__(self, urls=None, lan=True):
        if isinstance(urls, str):
            urls = urls.split(",")
        self.urls = [u.strip() for u in urls or [] if u.strip()]
        self.lan = lan
        self.lan_url = None  # last server found on the wifi, tried first next time
        self.current = None  # (url, "configured" or "wifi") after the last find()

    @staticmethod
    def answers(url):
        try:
            call(url, "/api/health", timeout=PING_TIMEOUT)
            return True
        except (OSError, ServerError, ValueError):
            return False

    def find(self):
        found = next(((url, "configured") for url in self.urls if self.answers(url)), None)
        if found is None and self.lan:
            if not (self.lan_url and self.answers(self.lan_url)):
                urls = discovery.find_servers()
                self.lan_url = urls[0] if urls else None
            if self.lan_url:
                found = (self.lan_url, "wifi")
        self.current = found
        return found[0] if found else None

    def describe(self):
        choices = self.urls + (["a server on the wifi"] if self.lan else [])
        return ", or ".join(choices) or "nothing (offline only)"


class SyncLoop:
    """One step at a time, so the background thread stays a plain loop."""

    def __init__(self, db, finder, global_db=None):
        self.db, self.finder, self.global_db = db, finder, global_db
        self.url = None  # the server we're connected to, if any

    def step(self):
        """Sync once, looking for a server first if we have none.
        Returns how long to wait before the next step."""
        if self.url is None:
            self.url = self.finder.find()
            if self.url is None:
                return CHECK_EVERY
            print(f"Connected to {self.url} ({self.finder.current[1]})")
        try:
            merged = sync_once(self.db, self.url, self.global_db)
        except (OSError, ServerError, ValueError) as exc:
            print(f"Server sync failed: {exc}")
            self.url = self.finder.current = None
            return CHECK_EVERY
        if merged and any(merged.values()):
            print(f"Synced with server: {merged}")
        return SYNC_EVERY


def start(db, finder, global_db=None):
    """Sync with the server in a background thread for as long as the app runs."""
    sync = SyncLoop(db, finder, global_db)

    def loop():
        while True:
            time.sleep(sync.step())

    thread = threading.Thread(target=loop, name="server-sync", daemon=True)
    thread.start()
    return thread
