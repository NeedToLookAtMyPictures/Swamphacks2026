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

For development without gunicorn: `python server.py` (http://127.0.0.1:8000, debug
mode), or `python server.py --host 0.0.0.0` to be reachable on the wifi.

## Viewing the database

Open the server's address in a browser (http://localhost:8000 on the machine
running it). The page shows every table: `users` and the four shared tables,
newest first, refreshing every 5 seconds. Like the rest of the API it has no
login, so anyone who can reach the server can see it.

## Connecting devices

**Same wifi:** nothing to configure. The server answers UDP broadcasts on port
8765 (`user_app/discovery.py`), and a user app started without `--server` asks
the wifi for a server every time it syncs. Some networks (many campus and hotel
wifis) block devices from talking to each other; there, use the server's address
instead. Set `LAN_DISCOVERY=0` to stop the server answering.

**By address:**

```bash
python main.py --server http://<server-ip>:8000
```

With `--server`, that server is tried first and one on the wifi is the fallback
(`--no-lan` turns the fallback off).

## A public URL anyone can reach

`serve_public.sh` starts the server and a tunnel that gives it an internet
address, so devices sync from any network (including wifi that blocks devices
from reaching each other). The database stays on this machine. Ctrl+C stops both.

**https://heuslerene.com/server (Cloudflare named tunnel, the default):**
`./serve_public.sh` runs the tunnel `swamphacks`. The server answers both
`/server/...` and `/...`, so the same server works at the public URL and at its
wifi address (`URL_PREFIX` changes the prefix). One-time setup on this machine:

```bash
sudo pacman -S cloudflared
cloudflared tunnel login              # opens a browser: pick heuslerene.com
cloudflared tunnel create swamphacks
```

Then point the domain at the tunnel, depending on what heuslerene.com serves now:

- **Nothing else:** `cloudflared tunnel route dns --overwrite-dns swamphacks heuslerene.com`.
  The whole domain goes to this server; `/server` is the sync URL.
- **Another website:** routing the whole domain would replace that site, so give
  the tunnel a subdomain and forward only `/server` to it with a Cloudflare Worker:
  1. `cloudflared tunnel route dns swamphacks swamphacks.heuslerene.com`
  2. In the Cloudflare dashboard, create a Worker with the code below and add the
     route `heuslerene.com/server*` to it (Worker → Settings → Domains & Routes):

     ```js
     export default {
       fetch(request) {
         const url = new URL(request.url);
         url.hostname = "swamphacks.heuslerene.com";  // keeps the /server/... path
         return fetch(new Request(url, request));
       },
     };
     ```

Check from a phone on mobile data: https://heuslerene.com/server/api/health
should show `{"ok":true,...}`. The user app tries this URL first by default
(`PUBLIC_SERVER_URL` in `user_app/main.py`).

**Quick, no account (cloudflared):** the URL is random and changes every run.

```bash
sudo pacman -S cloudflared      # Arch; other systems: see Cloudflare's cloudflared docs
CLOUDFLARE_TUNNEL= ./serve_public.sh   # prints https://<random-words>.trycloudflare.com
```

**Same URL every run (ngrok):** sign up for a free account at ngrok.com, install
ngrok (`yay -S ngrok` on Arch), run `ngrok config add-authtoken <token>` once,
and claim your free static domain in the ngrok dashboard (Domains). Then:

```bash
NGROK_DOMAIN=<name>.ngrok-free.app ./serve_public.sh
```

With either of these, start apps with `--server https://<url>`, or put the URL
in `PUBLIC_SERVER_URL` in `user_app/main.py`.

There is no login: anyone with the URL can read every message on the database
page and add records through the API.

## How syncing works

While the user app runs, it looks for a server every 30 seconds
(`user_app/server_sync.py`). Offline, the request fails quietly. As soon as a
server answers, the app:

1. sends every record that **arrived on the device** since its last push. This
   includes records it got from other people, however old they are;
2. gets every record that arrived on the server since the device's last pull;
3. merges them and saves both cursors in `device_info`.

Each cursor uses the clock of the machine it describes (`last_push` the device's,
`server_cursor` the server's), so the two clocks never need to agree. Every sync
also re-sends the last 60 seconds, so records written while a sync is running
aren't skipped. Merging is idempotent, so the repeats are harmless.

Every server has a random `server_id`, kept in its database. The device saves the
ID its cursors belong to, so when it syncs with a different server (say the
internet one, then one on the wifi), it exchanges everything once instead of
missing records the new server never got.

The server is a relay: it has no user identity of its own and never marks messages
delivered. Delivery still happens on the receiver's device, and its `delivered`
tombstone travels back through the server to everyone else.

## API

| Method | Path | What it does |
|---|---|---|
| GET | `/` | page showing the whole database |
| GET | `/api/health` | is the server up, and its `server_id` |
| POST | `/api/register` | reserve a name `{name, device_id?}` → `{name, device_id}`; 409 if taken |
| POST | `/api/sync` | `{device_id, name, cursor, records}` → `{cursor, records, merged, server_id}` |
| GET | `/api/users` | every registered name |
| GET | `/api/<table>?since=<time>` | `locales`, `messages`, `geolocations`, `tombstones` |

Creating an account in the user app (`POST /api/device`) needs a server: over
the internet with `--server`, or on the same wifi. The server reserves the name
and hands out the device ID, then the device syncs once so it starts with
everyone's data. With no server reachable, account creation is refused (503).
Devices created offline by older versions are still registered the first time
they sync.

Tests: `python -m unittest test_server`
