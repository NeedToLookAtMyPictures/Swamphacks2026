# store.py: the mesh's view of the app's database

**Purpose.** `AppStore` connects the mesh to the app's database
(`application/user_app/database.py`). There is **one** store per device, the
app's. The mesh doesn't keep a copy of the data and doesn't re-implement any of
its rules. `AppStore` only translates between app rows and mesh `Record`s.

```python
from database import Database          # the app's module
store = AppStore(Database("data/user.db"))
node = Node(link, store)
```

The app's device must already be set up (`POST /api/device`); otherwise
`AppStore` raises `ValueError`.

## What the app decides (not the mesh)

All through the app's public `Database` API:

| Rule | Where in the app |
|---|---|
| Messages expire 31 days after `time_created`; the sender and receiver keep theirs, marked dead | `purge()` |
| Expired messages are never shared | `export()` |
| A tombstone marks its target message dead (`is_dead = 1`) | `merge()` |
| A message addressed to this device creates a `delivered` tombstone | `merge()` |
| Broadcast messages (`receiver = "*"`) are never delivered or tombstoned | `merge()` |
| A dead message is only taken if it's ours | `merge()` |
| Tombstones are deleted 31 days after creation | `purge()` |
| Only the newest position per user is kept | `merge()` |

## API used by the session and node

| Method | Uses | Does |
|---|---|---|
| `inventory(cutoff)` | `export(since=cutoff)` | The manifest: `(id, kind, created)` of every record to offer, minus dead messages |
| `has(id)` | `export()` | True if we hold the record **or** a tombstone we hold has killed it |
| `get(id)` | `export()` | The record, to send it |
| `put(record)` | `merge()` | Hand a peer's record to the app; True if anything changed |
| `digest()` | `export()` | XOR of the ids of everything relayable |
| `last_server_sync` | `get_device()` / `update_device()` | From the app's `device_info` |
| `mesh_id` | `device_id` | The u32 used on the air (see below) |
| `device_id` | `device_id` | The app's device id string |

`has()` and `get()` read an index built from `export()`. It's rebuilt at the
start of every session (by `inventory`), by every `digest()`, and after every
`put()`, because the app writes to its database directly (new messages,
positions once a minute, comments).

## The sync cutoff

```
cutoff = min(my last_server_sync, peer's last_server_sync)
offer only records with time_created > cutoff
```

This mirrors the app's `GET /api/sync?since=<cutoff>`. Both devices synced
with the server after the cutoff, so both are assumed to already have
everything older.

**Open question (TODO in `inventory`):** should peer-to-peer sync be based on
when a record was last seen by the server, or when it was created? Comparing
creation time means a record **written offline before the cutoff but uploaded
after it** is never offered between peers. One device has it and the other
never gets it over the mesh, only from the server. Filtering on when the server
received the record would fix that, but the app and server don't record that time.

## Relaying tombstoned messages

The app marks a tombstoned message dead and keeps the row until the 31-day
purge (on the sender's and receiver's devices, for good). `export()` still
includes it with `is_dead = 1`. **The mesh doesn't offer dead messages:**
- a peer that holds the message already received the tombstone that killed it;
- a peer that doesn't hold it won't store a dead message that isn't its own
  (the app's `merge()` refuses it).

So sending it would only waste airtime. Dead messages are also left out of the
digest, so a device holding a dead copy and one that never had it still match.
The app's rows are untouched; only what goes on the air changes.

**Open question (TODO in `_relayable`):** should relays delete the tombstoned
messages they have stored? Today they keep them, dead, until day 31, because
that's what the app does.

## Device id on the air

Frames carry a u32 device id (the firmware filters on it). The app generates
`device_id` as 8 hex digits, which is used directly (`"0000002a"` → 42). Any
other string is hashed with CRC32. That only matters if two devices' strings
hash to the same value.

## Known gaps

- **`digest()` and `inventory()` read the whole database** through `export()`.
  That's fine for thousands of rows, not millions.
- **Stale positions are requested.** A peer's older position for a user has a
  new id, so we request it, and then the app's `merge()` discards it because
  ours is newer. It costs a little airtime.

## Assumptions

- **A tombstone outlives its message.** Both last 31 days, and a tombstone is
  always created after its message, so no device deletes a tombstone while a
  live copy of its message can still be around to come back. This holds as
  long as device clocks roughly agree.
