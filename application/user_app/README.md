# User App

Offline demo app. `main.py` runs the app: a small Flask server serves the web
interface and the downloaded map. Leaflet and protomaps-leaflet are in
`static/vendor/`, so nothing is loaded from the internet at runtime.
`download_locales.py` downloads everything the app needs offline (currently the
map; later default locale info such as trails and points of interest).

The map is a [Protomaps](https://protomaps.com) vector basemap built from
OpenStreetMap data. (OpenStreetMap's own tile servers don't allow downloading
tiles for offline use, so we don't use them.)

## Setup (needs internet once)

```bash
cd application/user_app
pip install -r requirements.txt
python download_locales.py map    # Gainesville to Hawthorne, FL, zoom 8-15
```

`download_locales.py map` finds the latest daily Protomaps world build, uses HTTP range
requests to pull only the tiles inside the area, and saves them as one file,
`data/map.pmtiles` (git-ignored). Running it again replaces the old map.

Other areas and sources:

```bash
python download_locales.py map --place "Ocala, Florida"
python download_locales.py map --bbox -82.42 29.59 -82.25 29.73
python download_locales.py map --source https://build.protomaps.com/20261015.pmtiles
python download_locales.py map --source some-local-file.pmtiles
```

## Run (works offline)

```bash
python main.py                       # http://127.0.0.1:5000
python main.py --host 0.0.0.0        # reachable from other devices on the LAN
python main.py --host 0.0.0.0 --https   # same, over HTTPS (needed for GPS, see below)
python main.py --server http://<server>:8000   # sync with another server
python main.py --server https://a.example,http://10.0.0.5:8000   # several, tried in order
python main.py --server ""           # no server address, only look on the wifi
python main.py --no-lan              # don't look for a server on the wifi
```

A background thread looks for a server every 30 seconds and syncs whenever one
answers: the ones given with `--server` (or `SERVER_URL`; several can be
comma-separated and are tried in order), by default the demo server's public URL
`https://heuslerene.com/server` (`PUBLIC_SERVER_URL` in `main.py`, see
`../server/README.md`) and then its wifi
address `http://10.138.191.107:8000` (`LAN_SERVER_URL`; it can change, so update
it there if syncing stops), or else one on the
same wifi, found automatically. The app keeps working offline. See
`../server/README.md`.

**Creating an account needs the server.** Names must be unique, and only the
server can guarantee that, so the Debug tab's "Create device" only works while
a server can be reached (internet or its wifi). The server reserves the name,
and the device syncs once right away.

### Raspberry Pi + phone over Bluetooth

When the app runs on a Raspberry Pi and a phone opens it in its browser over a
Bluetooth network connection, the position comes from the **phone's** GPS
through the browser. Browsers only allow that on `localhost` or HTTPS, so run
the Pi with `--https`:

```bash
python main.py --host 0.0.0.0 --https
```

Then on the phone open `https://<pi-ip>:5000` (`hostname -I` on the Pi shows
its IPs). The first run creates a self-signed certificate in `data/cert.pem` /
`data/key.pem` (needs the `openssl` command, preinstalled on Raspberry Pi OS).
There's no internet to get a real certificate, so the phone warns that the
connection isn't private: tap **Advanced → Proceed** once, then allow location
when asked. The certificate is kept, so it's only accepted once per phone;
delete the two files to make a new one (for example if the Pi's name changed).

## Database

`database.py` keeps a local SQLite database at `data/user.db` (git-ignored),
created automatically on first run. The tables follow the Database
Configuration issue:

| Table | Shared | Key | Notes |
|---|---|---|---|
| `locales` | yes | (origin_id, seq) | location reviews/comments |
| `messages` | yes | (origin_id, seq) | dead after 31 days or once delivered |
| `geolocations` | yes | user | newest position wins; ties go to the higher device ID |
| `tombstones` | yes | (origin_id, seq) | kills the target message; deleted after 31 days |
| `device_info` | **no** | single row | `device_id` and `name` are immutable; `next_seq`, `last_server_sync`, `last_push`, `server_cursor`, `server_id` |

Every record this device creates gets `origin_id = device_id` and the next
`seq`.

When a message addressed to you arrives (through `POST /api/sync` / `merge()`),
it is marked delivered automatically: your device creates a `delivered`
tombstone, which spreads and kills the message on every other device. When
that tombstone reaches the sender within 31 days of the message being sent, the
sender's copy is marked `received` (a local column, never shared); a message
that died any other way shows as "received or died", since the sender can't
tell which.

People are identified by their **name**, which is unique between people and
can't be changed (1-32 letters, digits, `_` or `-`, compared ignoring case).
Messages are addressed to a name, so you can send to someone you've
encountered or to any name at all; the message is passed from device to device
until the one with that name sees it. A message with receiver `*` goes to
**everyone**: nobody marks it delivered, so it spreads until it expires after 31
days. (`*` isn't a valid name, so no person can be called that.) Names are reserved on the server
when the account is created, which is why that needs a server.

A `data/user.db` from schema 2 or 3 is upgraded automatically; anything older has to be deleted. Expired messages are deleted, except on the sender's and receiver's
devices, where they're kept (marked dead) but no longer shared.

### API

All endpoints take and return JSON. Full list in `api.py`.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/device` | this device's info |
| POST | `/api/device` | create identity once: `{name}`; needs a server, which reserves the name and hands out the `device_id` (503 when none can be reached, 409 when the name is taken) |
| GET | `/api/server` | which server this device syncs with (`via`: `configured` or `wifi`) and when it last did |
| PATCH | `/api/device` | update mutable fields: `{last_server_sync}` |
| POST | `/api/locales` | `{location, name, comment?}` |
| POST | `/api/messages` | `{receiver, content}`: `receiver` is a name, or `*` for everyone |
| GET | `/api/contacts` | everyone this device knows about, with last known position |
| POST | `/api/messages/<origin_id>/<seq>/delivered` | receiver marks a message delivered (creates a tombstone) |
| PUT | `/api/location` | `{latitude, longitude}`: this user's current position |
| GET | `/api/<table>?since=<time>` | list `locales`, `messages`, `geolocations` or `tombstones` |
| GET | `/api/<table>/<origin_id>/<seq>` | one record |
| GET | `/api/sync?since=<time>` | everything this device shares (for another device or the server) |
| POST | `/api/sync` | merge records from another device or the server: `{locales: [...], messages: [...], ...}` |

```bash
curl -X POST localhost:5000/api/device -H 'content-type: application/json' -d '{"name": "Jordan"}'
```

### Global database

`global_database.py` keeps a second SQLite database at `data/global.db`
(git-ignored) for data set by the company. It is loaded once from the server
at setup; users never change it.

| Table | Key | Notes |
|---|---|---|
| `global_locales` | id | fixed places: `category` (water, campsite, ...), `name`, `latitude`, `longitude`, `description` |
| `global_trails` | id | routes: `name`, `description`, `gpx` (the GPX document as sent) |
| `global_info` | single row | `loaded_at`: when the data was loaded |

A global locale's `id` is what user `locales` records (reviews/comments) put in
their `location`.

The server sends `POST /api/global/load` with
`{locales: [...], trails: [{id, name, description?, gpx}]}`, which replaces
everything. GPX tracks (`trkseg`/`trkpt`) and routes (`rte`/`rtept`) are read;
a trail whose GPX has no line of at least 2 points is refused. Until the server
exists, load the example file (real Gainesville parks with made-up amenities: 24
locales in 6 categories, plus 3 approximate trails from `sample_trails/`; in a
file a trail can give `gpx_file` instead of the GPX text):

```bash
python global_database.py load sample_global_locales.json
```

| Method | Path | What it does |
|---|---|---|
| GET | `/api/global/locales?category=` | global locales, sorted by category then name |
| GET | `/api/global/categories` | each category with its count |
| GET | `/api/global/trails` | trails with their line parsed from the GPX (`segments` of `[lat, lon]`) and `length_km` |
| GET | `/api/global/trails/<id>.gpx` | a trail's original GPX file |
| GET | `/api/global/info` | when the global data was loaded |
| POST | `/api/global/load` | load the data from the server (replaces everything) |
| GET | `/api/distances?from=&to=` | walking distance along the trails to global locales, nearest first (see below) |

### Distances along trails

Distances to locales are measured the way a hiker walks them
(`trail_distance.py`): straight from where you are to the nearest point on a
trail, along the global trails (shortest path), then straight from the trail to
the locale. Trails are connected where they share a point, and where the end of
one comes within 30 m of another. A locale on a trail that isn't connected to
yours gets only a straight-line distance.

`GET /api/distances` measures from your last position (`PUT /api/location`), or
`from=<global locale id>` or `from=<lat>,<lon>`; `to=<global locale id>` limits
it to one locale. Each locale comes back with `distance` (total), `to_trail`,
`along_trail`, `from_trail` and `straight`, all in meters (`null` when no trail
connects), and `path`, the route as `[lat, lon]` points.

Tests: `python -m unittest test_database test_trail_distance`

## Location

While the app is open, `static/location.js` reads the device's position from
the browser once a minute and saves it with `PUT /api/location`. This works
offline on devices with GPS (phones); laptops without GPS usually need internet
to locate themselves. Browsers only allow geolocation on `localhost` or HTTPS,
so opening the app from another device over plain `http://<lan-ip>` won't report
a position; start the server with `--https` for that (see Raspberry Pi above). Updates pause while the tab is hidden or the phone is locked.

## Tabs

- **Messages**: a sidebar with one conversation per person you've messaged with, with Everyone pinned to the top; click one to see its history and send to that person. The New chat dropdown lists everyone in your GPS data (newest first), or Other to type any name
- **Map**: offline Leaflet map. The gear button opens Settings:
  - **Last known location**: search people and check who to pin, with their name and when they were last seen
  - **Trails**: select a global trail (with search) to draw it on the map and zoom to it; only the selected trail is shown
  - **Locales**: the global locales (shown by default), grouped by category; show or hide a whole category or single locales, with search
  - Clicking a global locale shrinks the map to the top half and opens a panel below it with information about the site (description, coordinates, distance along the trails from your last location, comment count) and its comments, newest first, with a box to add one. Comments are user `locales` records with `location` set to the site's id, so they sync like other records
- **Debug**: shows every table in the local and global databases (refresh button; offers to create the device if it has no identity yet)
