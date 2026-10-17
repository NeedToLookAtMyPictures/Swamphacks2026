# records.py: the data model

**Purpose.** Defines `Record`, the unit of data the mesh moves. Each record is
**one row of one shared table of the app's database**
(`application/user_app/database.py`). The mesh doesn't have its own data model:
the app owns the tables and every rule about them, and the mesh only carries rows
between devices.

## Kinds

| Kind | App table | What it is |
|---|---|---|
| `LOCALE` (1) | `locales` | A review/comment on a location (water source, campsite) |
| `MESSAGE` (2) | `messages` | Text to a person's name, or `*` for everyone |
| `GEOLOCATION` (3) | `geolocations` | A user's position; the app keeps only the newest per user |
| `TOMBSTONE` (4) | `tombstones` | Kills a message everywhere (`reason` e.g. `delivered`) |

`PRIORITY` sets the order in which records are **sent** (lower goes first):

| Kind | Priority | Why |
|---|---|---|
| LOCALE | 0 | Water and campsite reports are the safety-relevant data |
| MESSAGE | 1 | |
| GEOLOCATION | 2 | |
| TOMBSTONE | 3 | Cleanup, least urgent |

So a hiker who walks out of range mid-exchange already has the important
records. Within a kind, newer records go first (see [session.md](session.md)).

## Fields

`record.data` is the app's row as a dict, with exactly the app's column names,
minus `time_received` (local bookkeeping that the app never shares either).

| Kind | Columns |
|---|---|
| LOCALE | `origin_id, seq, time_created, location, name, sender, comment` |
| MESSAGE | `origin_id, seq, time_created, sender, receiver, is_dead, content` |
| GEOLOCATION | `user, origin_id, time_created, latitude, longitude` |
| TOMBSTONE | `origin_id, seq, target_origin_id, target_seq, time_created, reason` |

**If a shared table's columns change in the app, `FIELDS` in `records.py` must
change with it.** `Record.from_row(table, row)` builds a record from an app row.

## Record id

The app identifies a row by `(origin_id, seq)`: the device that created it and
that device's counter. The mesh needs a fixed-size id for its manifests, so:

```
LOCALE, MESSAGE, TOMBSTONE:  id = SHA-256(kind, origin_id, seq)[:8]
GEOLOCATION:                 id = SHA-256(kind, lower(user), origin_id, time_created)[:8]
```

- Every copy of the same row has the same id, even if a mutable column differs.
  A message marked dead (`is_dead = 1`) keeps its id.
- A geolocation has no `seq`: the app replaces a user's row whenever they move.
  So each position gets its own id, and a peer requests a new position as a new record.
- A tombstone's target id can be computed from `target_origin_id` and `target_seq`,
  which is how the store recognises a message that a tombstone has killed
  ([store.md](store.md)).
- The id is **not** a hash of the contents, so it can't detect corrupted bytes.
  The session adds a CRC32 for that ([session.md](session.md)).

## Serialization (`encode` / `decode`)

```
kind u8 | the kind's fields in the order above
```

| Field type | Encoding |
|---|---|
| ids, names, `location`, `reason` | u16 length + UTF-8 |
| `comment`, `content` | u32 length + UTF-8 |
| `seq`, `target_seq` | u32 |
| `is_dead` | u8 |
| times, latitude, longitude | f64 (exactly the app's REAL value) |

All little-endian. `decode` raises `ValueError` or `struct.error` on malformed
input: an unknown kind, a field running past the end, bytes left over, or bad
UTF-8. `encode` raises `struct.error` if a value doesn't fit its field
(`seq` over 2³², a short string over 64 KB), and the session skips that record.

## Assumptions

- `time_created` comes from the device clock. A device whose clock is far off
  makes its messages expire early or late, and can win "newest position" ties.
- `origin_id`, `sender` and `user` are trusted. Nothing proves who wrote a record.

## Known gaps

- **No signatures.** Anyone can write a record claiming any sender, including
  a tombstone that kills someone else's message.
