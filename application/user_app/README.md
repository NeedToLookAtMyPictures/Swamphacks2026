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
```

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
| `device_info` | **no** | single row | `device_id` and `name` are immutable; `next_seq`, `last_server_sync` |

Every record this device creates gets `origin_id = device_id` and the next
`seq`.

When a message addressed to you arrives (through `POST /api/sync` / `merge()`),
it is marked delivered automatically: your device creates a `delivered`
tombstone, which spreads and kills the message on every other device.

People are identified by their **name**, which is unique between people and
can't be changed (1-32 letters, digits, `_` or `-`, compared ignoring case).
Messages are addressed to a name, so you can send to someone you've
encountered or to any name at all; the message is passed from device to device
until the one with that name sees it. A message with receiver `*` goes to
**everyone**: nobody marks it delivered, so it spreads until it expires after 31
days. (`*` isn't a valid name, so no person can be called that.) Offline, the app can only refuse names
it has already seen, so real uniqueness has to come from the server at setup.

If you have a `data/user.db` from before names replaced `user_id`, delete it. Expired messages are deleted, except on the sender's and receiver's
devices, where they're kept (marked dead) but no longer shared.

### API

All endpoints take and return JSON. Full list in `api.py`.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/device` | this device's info |
| POST | `/api/device` | create identity once: `{name, device_id?}` (`device_id` is generated if left out) |
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

Tests: `python -m unittest test_database`

## Tabs

- **Messages**: a sidebar with one conversation per person you've messaged with, with Everyone pinned to the top; click one to see its history and send to that person. The New chat dropdown lists everyone in your GPS data (newest first), or Other to type any name
- **Map**: offline Leaflet map. The gear button opens Settings:
  - **Last known location**: search people and check who to pin, with their name and when they were last seen
  - **Trails**: select a global trail (with search) to draw it on the map and zoom to it; only the selected trail is shown
  - **Locales**: the global locales (shown by default), grouped by category; show or hide a whole category or single locales, with search
  - Clicking a global locale shrinks the map to the top half and opens a panel below it with information about the site (description, coordinates, distance from your last location, comment count) and its comments, newest first, with a box to add one. Comments are user `locales` records with `location` set to the site's id, so they sync like other records
- **Debug**: shows every table in the local and global databases (refresh button; offers to create the device if it has no identity yet)
