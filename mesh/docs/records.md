# records.py: the data model

**Purpose.** Defines `Record`, the single unit of data that gets stored, gossiped
and synced. Everything the protocol moves (locale updates, messages, pins,
images, maps) is a `Record`.

## Kinds and priority

```python
class Kind(IntEnum):
    LOCALE_UPDATE = 1   # water level, campsite quality, hazards...
    MESSAGE = 2         # free text plus the sender's position
    PIN = 3             # a hiker's position and heading at a moment
    IMAGE = 4
    MAP = 5
```

`PRIORITY` sets the order in which records are **sent** during an exchange
(lower goes first):

| Kind | Priority | Why |
|---|---|---|
| LOCALE_UPDATE | 0 | Safety-critical, small |
| MESSAGE | 1 | Small |
| PIN | 2 | Small, for search and rescue |
| IMAGE | 3 | Bulk |
| MAP | 4 | Largest |

So a hiker who walks out of range mid-exchange already has the important
records. Within the same kind, newer records go first (see [session.md](session.md)).

## Fields

| Field | Type | Meaning |
|---|---|---|
| `kind` | `Kind` | One of the above |
| `origin` | u32 | Device id of the **author** (not of whoever relayed it) |
| `created` | u32 | Unix seconds when it was authored (from the synced clock) |
| `body` | bytes | Content. Its layout depends on the kind, see below. |
| `key` | bytes, 0–255 bytes | **Supersede key.** A newer record with the same `(kind, key)` replaces older ones. Empty means it is never replaced. |
| `server_time` | u32 | When the server first saw the record. 0 means not yet. |

## Record id

```
id = SHA-256( kind u8 | origin u32 | created u32 | len(key) u8 | key | body )[:8]
```

- 8 bytes (64 bits). Duplicates are harmless: the same content always gives the
  same id.
- **`server_time` is excluded on purpose.** A record gets its `server_time`
  later, when it reaches the server, and its id must not change when that
  happens. So the same record seen before and after server upload is still one
  record.
- Computed once per object (`cached_property`).

## Serialization (`encode` / `decode`)

```
kind u8 | origin u32 | created u32 | server_time u32 | key_len u8 | key | body
```

All values are little-endian. The body has no length field; it runs to the end
of the buffer. That works because the receiver reassembles the exact byte string
first (see DATA chunks in [session.md](session.md)).

`decode(data)` is the inverse. It does **not** validate anything (see Known
gaps). The receiving session recomputes the id and checks that it equals the id
it asked for, and that check is the integrity check.

## Constructors

| Call | key | body |
|---|---|---|
| `Record.locale_update(origin, created, locale_id, field, text)` | `"{locale_id}:{field}"` | UTF-8 text |
| `Record.message(origin, created, text, lat, lon)` | empty | position struct (heading 0) + UTF-8 text |
| `Record.pin(origin, created, lat, lon, heading)` | empty | position struct |
| `Record(Kind.IMAGE / MAP, origin, created, raw_bytes)` | your choice | raw bytes |

Position struct: `lat*1e7 i32 | lon*1e7 i32 | heading u16` (10 bytes, about 1 cm resolution).

Helpers: `rec.position()` returns `(lat, lon, heading)` for PIN and MESSAGE
records. `rec.text()` returns the text of a MESSAGE or LOCALE_UPDATE.

## How overrides work (the "supersede key")

The issue says new information overrides older data that overlaps it. The code
defines "overlap" as **same kind and same key**:

- `locale_update(..., "spring-1203", "water", ...)` has key `spring-1203:water`.
  A newer water report for the same spring replaces the older one. A
  `spring-1203:campsite` report is a different key and is left alone.
- Messages and pins have an empty key, so they **accumulate** and are never
  replaced. This matches "every device stores all the pins it has ever received."

The replacement itself happens in the store; see [store.md](store.md).

## Assumptions

- `created` comes from a synced clock. If a device's clock is far ahead, its
  locale updates win every override comparison until real time catches up.
- `origin` is trusted. Nothing proves who wrote a record.
- Images and maps are opaque bytes. No format, thumbnail or compression handling.
- There is no expiry or TTL field.

## Known gaps

- **`decode` can raise** (`struct.error`, or `ValueError` for an unknown kind) on
  malformed bytes. The only caller is the session's DATA handler, so the
  exception propagates out of `Node.poll()`. In practice it needs a buggy or
  malicious peer, because the data arrives over a reliable channel and only for
  ids we requested. The fix is a try/except around the decode in `session.py`.
- **Keys longer than 255 bytes** make `encode` raise, because the length is
  stored in one byte. Nothing enforces this when the record is created.
- **No signatures.** Anyone can author a record with any `origin`.
