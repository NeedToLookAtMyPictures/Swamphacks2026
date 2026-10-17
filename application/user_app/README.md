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
python download_locales.py map    # Gainesville, FL, zoom 8-15 (a few MB)
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
(git-ignored) for data set by the company and synced from the server. Users
never change it: the only writer is the server sync.

| Table | Key | Notes |
|---|---|---|
| `global_locales` | id | fixed places: `category` (water, campsite, ...), `name`, `latitude`, `longitude`, `description`, `time_updated` |
| `global_sync` | single row | `server_time` of the last sync (send back as `since`), `synced_at` |

A global locale's `id` is what user `locales` records (reviews/comments) put in
their `location`.

The server sends `POST /api/global/sync` with
`{server_time, full?, locales: [...]}`. With `full: true` everything is
replaced; otherwise each locale is added or updated (if its `time_updated` is
not older than ours), and one with `"deleted": true` is removed.

Until the server exists, load a file in the same format:

```bash
python global_database.py load sample_global_locales.json   # full: 24 locales in 6 categories
python global_database.py load sample_global_update.json    # then an incremental update
```

The example places are real Gainesville parks, but their amenities are made up.
The update changes one locale (Bivens Arm pump marked broken), adds two
(Paynes Prairie north camp and tap), deletes one (Prairie rim camp), and
includes a stale edit to Depot Park fountain that is ignored because it is
older than what the device has. Load `sample_global_locales.json` again to reset.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/global/locales?category=` | global locales, sorted by category then name |
| GET | `/api/global/categories` | each category with its count |
| GET | `/api/global/sync` | when the global data was last synced |
| POST | `/api/global/sync` | apply data from the server |

Tests: `python -m unittest test_database`

## Tabs

- **Messages**: a sidebar with one conversation per person you've messaged with, with Everyone pinned to the top; click one to see its history and send to that person. The New chat dropdown lists everyone in your GPS data (newest first), or Other to type any name
- **Map**: offline Leaflet map. The gear button opens Settings:
  - **Last known location**: search people and check who to pin, with their name and when they were last seen
  - **Locales**: the global locales (shown by default), grouped by category; show or hide a whole category or single locales, with search
- **Debug**: shows every table in the local and global databases (refresh button; offers to create the device if it has no identity yet)
