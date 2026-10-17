# store.py: the mesh's view of the app's database

**Purpose.** `AppStore` connects the mesh to the app's database
(`application/user_app/database.py`). There is **one** store per device, the
app's. The mesh doesn't keep a copy of the data and doesn't re-implement the
app's rules. `AppStore` translates between app rows and mesh `Record`s, and
adds two mesh rules of its own: relays delete tombstoned messages, and the sync
cutoff counts when a row was received (both below).

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
| A tombstone marks its target message dead (`is_dead = 1`); the mesh then deletes it on relays (below) | `merge()` |
| A message addressed to this device creates a `delivered` tombstone | `merge()` |
| Broadcast messages (`receiver = "*"`) are never delivered or tombstoned | `merge()` |
| A dead message is only taken if it's ours | `merge()` |
| Tombstones are deleted 31 days after creation | `purge()` |
| Only the newest position per user is kept | `merge()` |

## API used by the session and node

| Method | Uses | Does |
|---|---|---|
| `inventory(cutoff)` | `export()`, `list()` | The manifest: `(id, kind, created)` of every record created or received after the cutoff, minus dead messages |
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
offer a record if this device created OR received it after the cutoff
```

Example: A last synced with the server 2 days ago and B 1 day ago, so the
cutoff is 2 days ago. Both got everything older from the server. After that:
- B gets everything A created or received in the last 2 days, including what A
  picked up while offline, from other peers or written by hand.
- A gets everything B created or received in that time, including what B
  downloaded from the server 1 day ago. That data may have been *created* long
  before; it's offered because B *received* it after the cutoff.

"Received" is the app's local `time_received` column, set whenever a row is
stored (from a peer, the server, or written on this device). `export()`
doesn't include it, so `AppStore` reads it with `list()`.

**This differs from the app's own `GET /api/sync?since=`**, which filters on
`time_created` only. The mesh doesn't use that endpoint, but anything else that
does (such as device-to-server sync) still has the old behavior.

## Relays delete tombstoned messages

A **relay** is a device that is neither the sender nor the receiver of a
message. When a tombstone reaches a relay, the relay **deletes the message**
but keeps the tombstone and keeps passing it on to every peer it meets, so the
kill keeps spreading. Only the sender and the receiver keep the message, marked
dead.

The app only marks tombstoned messages dead and has no call to delete one, so
`AppStore` deletes them itself with one SQL statement on the app's database
file (`_delete_tombstoned_relays`, run every time `AppStore` re-reads the
database). It spares the sender and receiver the same way the app's `purge()`
does. The app's code is unchanged.

A deleted message can't come back:
- `has()` still recognizes its id through the tombstone, so it's never requested;
- the app's `merge()` refuses a tombstoned message that isn't ours.

Messages marked dead **without** a tombstone are left alone, since nothing would
stop a peer's live copy from bringing them back.

The sender's and receiver's dead copies aren't offered either: a peer that holds
the message already got the tombstone, and a peer that doesn't won't store a
dead message that isn't its own. Dead messages are also left out of the digest.

## Device id on the air

Frames carry a u32 device id (the firmware filters on it). The app generates
`device_id` as 8 hex digits, which is used directly (`"0000002a"` → 42). Any
other string is hashed with CRC32. That only matters if two devices' strings
hash to the same value.

## Known gaps

- **`digest()` and `inventory()` read the whole database** through `export()`
  and `list()`. That's fine for thousands of rows, not millions.
- **Relay deletion depends on the app's schema.** The delete statement names the
  app's `messages` and `tombstones` columns. If those change, or the app adds
  its own delete call, `_delete_tombstoned_relays` must follow.
- **A tombstone that arrives through the server** (the app's `POST /api/sync`)
  only marks the message dead until the mesh next reads the database (the next
  beacon or session), which then deletes it.
- **Stale positions are requested.** A peer's older position for a user has a
  new id, so we request it, and then the app's `merge()` discards it because
  ours is newer. It costs a little airtime.

## Assumptions

- **A tombstone outlives its message.** Both last 31 days, and a tombstone is
  always created after its message, so no device deletes a tombstone while a
  live copy of its message can still be around to come back. This holds as
  long as device clocks roughly agree.
