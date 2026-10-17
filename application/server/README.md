# Server

Sync server for the user app. Any device that can reach it sends what it has and
gets back what other devices sent, so data spreads even between people who never
meet. It reuses the user app's `database.py`, keeping the same shared tables in
`data/server.db` (git-ignored), plus a `users` table that keeps names unique.

There is no authorization: anyone who can reach the server can sync.

## Run

```bash
cd application/server
pip install -r requirements.txt
gunicorn server:app            # http://0.0.0.0:8000
```

The settings are in `gunicorn.conf.py`, and gunicorn picks it up automatically
when run in this folder. Override them with environment variables:

| Variable | Default | |
|---|---|---|
| `BIND` | `0.0.0.0:8000` | address and port |
| `WORKERS` | `1` | SQLite allows one writer at a time, so keep this at 1 |
| `THREADS` | `8` | requests handled at once |
| `TIMEOUT` | `120` | seconds; a device's first sync can be large |
| `SERVER_DB` | `data/server.db` | database file |

For development without gunicorn: `python server.py` (http://127.0.0.1:8000, debug mode).

Then point the user app at it:

```bash
python main.py --server http://<server-ip>:8000
```

To reach the server from outside your network, run it on a VPS, or expose it
with a tunnel such as `cloudflared tunnel --url http://localhost:8000` or
`ngrok http 8000` and pass the URL the tunnel prints.

## How syncing works

While the user app runs with `--server`, it tries the server every 30 seconds
(`user_app/server_sync.py`). Offline, the request fails quietly. As soon as the
server answers, the app:

1. sends every record that **arrived on the device** since its last push. This
   includes records it got from other people, however old they are;
2. gets every record that arrived on the server since the device's last pull;
3. merges them and saves both cursors in `device_info`.

Each cursor uses the clock of the machine it describes (`last_push` the device's,
`server_cursor` the server's), so the two clocks never need to agree. Every sync
also re-sends the last 60 seconds, so records written while a sync is running
aren't skipped. Merging is idempotent, so the repeats are harmless.

The server is a relay: it has no identity of its own and never marks messages
delivered. Delivery still happens on the receiver's device, and its `delivered`
tombstone travels back through the server to everyone else.

## API

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | is the server up |
| POST | `/api/register` | reserve a name `{name, device_id?}` → `{name, device_id}`; 409 if taken |
| POST | `/api/sync` | `{device_id, name, cursor, records}` → `{cursor, records, merged}` |
| GET | `/api/users` | every registered name |
| GET | `/api/<table>?since=<time>` | `locales`, `messages`, `geolocations`, `tombstones` |

When the user app creates its identity (`POST /api/device`) with a server
configured, it registers the name on the server first. If the server can't be
reached, the device is created offline and its name is registered the first
time it syncs.

Tests: `python -m unittest test_server`
