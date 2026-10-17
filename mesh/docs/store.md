# store.py: RecordStore

**Purpose.** Durable storage for records, plus the three questions the protocol
asks about local data:
1. Do I already have this id? (`has`)
2. What may I offer this peer? (`inventory(cutoff)`)
3. Is my whole set identical to theirs? (`digest`)

Backed by SQLite. `RecordStore()` is in-memory; `RecordStore("trail.db")`
persists to a file.

## Tables

| Table | Columns | Purpose |
|---|---|---|
| `records` | id, kind, origin, created, server_time, key, body, received | The records themselves. `received` (local arrival time) is written but never read. |
| `tombstones` | id | Ids of records that were replaced, or rejected because something newer existed. Kept so we never request them again. |
| `meta` | k, v | Holds `last_server_sync` |

Index: `records(kind, key)`, used for override lookups.

## API

| Method | Returns | Behaviour |
|---|---|---|
| `has(id)` | bool | True if the id is in `records` **or** `tombstones` |
| `get(id)` | Record or None | Only real records; tombstones return None |
| `all(kind=None)` | list[Record] | Ordered by `created`. For the app/UI. |
| `inventory(cutoff)` | list of (id, kind, created) | Records with `server_time == 0 or server_time > cutoff` |
| `digest()` | 8 bytes | XOR of all record ids (cached) |
| `put(record)` | bool | Stores the record. True only if it was new and kept. |
| `last_server_sync` | property, int | Read and write. 0 if never set. |
| `close()` | | Closes the DB |

## `put(record)`: control flow

```
rid = record.id
if rid in records:
    if the incoming copy has server_time and the stored one doesn't: fill it in
    return False                          # duplicate
if rid in tombstones:
    return False                          # superseded earlier; ignore forever
if record.key is not empty:
    old = the record with the same (kind, key), if any
    if old exists:
        if (old.created, old.id) > (record.created, rid):
            tombstone rid; return False    # what we have is newer
        delete old; tombstone old.id       # incoming is newer: replace
insert record
invalidate digest cache
return True
```

Points to check against your model:
- **Newest `created` wins.** If both have the same timestamp, the larger id
  wins. That's arbitrary but deterministic, so every device makes the same choice.
- **Replaced content is gone.** Only the id survives, as a tombstone. Issue #2
  left "keep old data for record keeping?" open. Keeping history would mean
  moving old rows to an archive table instead of deleting them.
- **The incoming record's `server_time` is kept as is**, including when the
  record came from a peer and not from the server (see the assumptions below).

## The cutoff rule (`inventory`)

Issue #2: *"The earlier of the two times becomes the cutoff. Packets from before
the cutoff are skipped, because both devices are assumed to already have them
from the server."*

What "packets from before the cutoff" means matters a lot here:

- **Filtering by `created` (what the issue literally says) loses data.** Hiker
  C writes "bridge out" on day 1 with no signal and uploads it in town on
  day 5. Hikers A and B both synced on day 3, so cutoff = day 3. A got the
  bridge report over the mesh. A `created < cutoff` rule skips it, but neither
  A's nor B's server download could have contained it.
- **The code filters by `server_time`.** A record is skipped only if the server
  had it at or before the cutoff. Both peers downloaded from the server at or
  after the cutoff, so both must have it. Anything the server hasn't seen
  (`server_time == 0`) is always offered.

Requirement for the server: stamp each record with `server_time` when it first
arrives, and include that field in downloads. Then the app should:

```python
for rec in downloaded:
    store.put(rec)                     # rec.server_time set by server
store.last_server_sync = download_time
```

## The digest

```
digest = XOR over all ids in `records` (as 64-bit integers), as 8 bytes
```

- It doesn't depend on order, so two devices holding the same set get the
  same digest no matter what order the records arrived in.
- Beacons carry it. Equal digests mean "nothing to exchange", so the session is skipped.
- Tombstones are **not** included. Two devices with the same live records match
  even if their tombstone lists differ.
- It covers the whole set, not just records past the cutoff. Two devices whose
  only difference is old server data still sync. That's harmless: INV is
  limited by the cutoff, and the session finds nothing to send.
- **XOR has a weakness**: the XOR of a different set can collide. That's
  astronomically unlikely by chance, but it isn't adversary-proof. A hash of
  the sorted ids would fix that, at the cost of rescanning on every change.

## Assumptions

- **A peer's `server_time` claims are trusted.** A buggy or malicious peer
  could set `server_time` to an old value so that other devices skip the
  record. With no signatures, there's no way to check.
- **Thread safety**: every call takes a lock, and the connection allows
  cross-thread use. So the UI thread can call `put` while the node loop runs.
- **The size of a full table scan is fine.** `digest()` and `inventory()` scan
  the table. Thousands of rows is fine; millions is not.

## Known gaps

- **No expiry.** Records and tombstones grow forever. You'll want a TTL per
  kind ("water reports expire after 3 weeks") for long trips.
- Images and maps are stored in SQLite as BLOBs. That's fine for kilobytes,
  but awkward for many megabytes.
- `received` is stored but never used.
