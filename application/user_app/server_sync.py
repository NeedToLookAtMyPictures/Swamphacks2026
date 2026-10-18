"""Background sync with the server.

While the app is running, a thread looks for a server every CHECK_EVERY seconds:
the ones given with --server, or else one on the same wifi (see discovery.py).
Once it reaches one, it syncs with it every SYNC_EVERY seconds: the device sends
the records that arrived since its last push and merges what the server got
since its last pull. When the server's global data (water sources, campsites,
trails) differs from the device's copy, it comes along and replaces it, and the
same for the offline map, which is downloaded separately since it's large. If a
sync fails, it goes back to looking. Writing something locally (wake()) or
receiving something new syncs again right away instead of waiting.

Only the standard library is used, so the app has no extra dependency.
"""

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import discovery

SYNC_EVERY = 5     # seconds between syncs while a server answers
CHECK_EVERY = 30   # seconds between looking for a server while there is none
TIMEOUT = 15       # seconds, for a sync
PING_TIMEOUT = 3   # seconds, for checking whether a server is there
MAP_TIMEOUT = 120  # seconds without data before giving up on a map download

HEADERS = {
    # Cloudflare (in front of the public server) turns away urllib's default
    # "Python-urllib/3.x" with a 403, though browsers get through
    "user-agent": "trailmesh-app/1.0",
    # free ngrok tunnels show browsers a warning page first
    "ngrok-skip-browser-warning": "1",
}

# Set to sync right away instead of at the next SYNC_EVERY, e.g. after sending a message
_wake = threading.Event()


def wake():
    """Sync now: something was written here that other devices should get soon."""
    _wake.set()


class ServerError(Exception):
    """The server answered, but with an error."""


def call(server_url, path, payload=None, timeout=TIMEOUT):
    """GET path, or POST payload to it as JSON. Returns the JSON answer."""
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(server_url.rstrip("/") + path, data=data,
                                     headers=dict(HEADERS, **{"content-type": "application/json"}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc).get("error", exc.reason)
        except ValueError:
            message = exc.reason
        raise ServerError(f"{exc.code}: {message}") from exc


def download(server_url, path, dest, timeout=MAP_TIMEOUT):
    """Save the file at path to dest. Returns its SHA-256."""
    digest = hashlib.sha256()
    request = urllib.request.Request(server_url.rstrip("/") + path, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response, open(dest, "wb") as out:
        for chunk in iter(lambda: response.read(1 << 16), b""):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def map_version(map_dir):
    """The version of the map in map_dir (None if it has none, e.g. one from
    download_locales.py: then the server's map replaces it)."""
    tiles, info_file = Path(map_dir) / "map.pmtiles", Path(map_dir) / "map.json"
    if not (tiles.exists() and info_file.exists()):
        return None
    try:
        return json.loads(info_file.read_text()).get("version")
    except ValueError:
        return None


def update_map(server_url, offer, map_dir):
    """Download the map the server offered ({version, size, info}) into map_dir.

    The new file only replaces the old one once it's complete and checked, so the
    app keeps serving a working map the whole time.
    """
    map_dir = Path(map_dir)
    map_dir.mkdir(parents=True, exist_ok=True)
    tiles, info_file = map_dir / "map.pmtiles", map_dir / "map.json"
    part = tiles.with_suffix(".pmtiles.part")
    print(f"Downloading the server's map ({offer['size'] / 1e6:.1f} MB)")
    try:
        if download(server_url, "/api/map.pmtiles", part) != offer["version"]:
            raise ValueError("the downloaded map doesn't match its version")
        part.replace(tiles)
    finally:
        part.unlink(missing_ok=True)
    info_file.write_text(json.dumps(dict(offer["info"], version=offer["version"]), indent=2))
    print(f"Got the server's map: {offer['info'].get('name')}")


def register(server_url, name, device_id=None):
    """The startup handshake: ask the server to reserve a name. Returns {name, device_id}."""
    return call(server_url, "/api/register", {"name": name, "device_id": device_id})


def sync_once(db, server_url, global_db=None, map_dir=None):
    """One full exchange with the server. Raises OSError when it can't be reached.

    With global_db (a GlobalDatabase), also gets the server's global data when
    this device's copy is missing or out of date, and with map_dir (where the app
    keeps map.pmtiles and map.json) the same for the offline map."""
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
    if map_dir is not None:
        request["map_version"] = map_version(map_dir)
    result = call(server_url, "/api/sync", request)
    merged = db.merge(result["records"], via="server")
    if global_db is not None and result.get("global"):
        loaded = global_db.load(result["global"])
        print(f"Got the server's global data: {loaded}")
    db.update_device(last_server_sync=time.time(), last_push=push_cursor,
                     server_cursor=result["cursor"], server_id=server_id)
    if map_dir is not None and result.get("map"):
        # Last, so the records above are saved even if the download fails;
        # it's tried again on the next sync
        try:
            update_map(server_url, result["map"], map_dir)
        except (OSError, ValueError) as exc:
            print(f"Map download failed: {exc}")
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
        self.errors = {}  # url -> why it didn't answer at the last find()

    def answers(self, url):
        try:
            call(url, "/api/health", timeout=PING_TIMEOUT)
            self.errors.pop(url, None)
            return True
        except (OSError, ServerError, ValueError) as exc:
            self.errors[url] = getattr(exc, "reason", None) or exc
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

    def __init__(self, db, finder, global_db=None, map_dir=None):
        self.db, self.finder, self.global_db, self.map_dir = db, finder, global_db, map_dir
        self.url = None  # the server we're connected to, if any
        self.said_none = False  # already printed that no server answers

    def step(self):
        """Sync once, looking for a server first if we have none.
        Returns how long to wait before the next step."""
        if self.url is None:
            self.url = self.finder.find()
            if self.url is None:
                if not self.said_none:  # once, not every CHECK_EVERY
                    print(f"No server answers (tried {self.finder.describe()}); "
                          f"looking again every {CHECK_EVERY} seconds")
                    for url, error in self.finder.errors.items():
                        print(f"  {url}: {error}")
                    self.said_none = True
                return CHECK_EVERY
            self.said_none = False
            print(f"Connected to {self.url} ({self.finder.current[1]})")
        try:
            merged = sync_once(self.db, self.url, self.global_db, self.map_dir)
        except (OSError, ServerError, ValueError) as exc:
            print(f"Server sync failed: {exc}")
            self.url = self.finder.current = None
            return CHECK_EVERY
        if merged and any(merged.values()):
            print(f"Synced with server: {merged}")
            # Merging may have created records (a 'delivered' tombstone): send them now
            return 0
        return SYNC_EVERY


def start(db, finder, global_db=None, map_dir=None):
    """Sync with the server in a background thread for as long as the app runs."""
    sync = SyncLoop(db, finder, global_db, map_dir)

    def loop():
        while True:
            delay = sync.step()
            if delay and sync.url is not None:  # no point waking up early with no server
                _wake.wait(delay)
            else:
                time.sleep(delay)
            _wake.clear()

    thread = threading.Thread(target=loop, name="server-sync", daemon=True)
    thread.start()
    return thread
