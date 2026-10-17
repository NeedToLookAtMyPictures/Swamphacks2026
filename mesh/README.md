# trailmesh: gossip sync over ESP-NOW

Python implementation of the device-to-device protocol from issue #2 (ESP-NOW
part). The ESP32 is a thin radio bridge; all protocol logic runs in Python.

```
mesh/
  trailmesh/         protocol package (pure Python, stdlib only)
    records.py       Record = one row of the app's shared tables; wire encoding, ids, priorities
    store.py         AppStore: the mesh's view of the app's database (inventory, digest, merge)
    wire.py          frame format shared by every link type
    reliable.py      seq / cumulative+selective ACK / retransmit / in-order delivery
    session.py       one exchange: HELLO -> INV -> WANT -> DATA -> DONE
    node.py          duty-cycled discovery, beacons, one session at a time, cooldowns
    link.py          Link interface (ESP-NOW now, LoRa later)
    espnow_serial.py Link over USB serial to the bridge firmware (needs pyserial)
    sim.py           simulated radio + virtual clock for tests/demos
  firmware/espnow_bridge/  Arduino sketch for the ESP32 bridge
  examples/run_node.py     run a node against real hardware
  tests/                   python -m unittest discover -s tests
```

## Quick start

```bash
pip install -r requirements.txt
# flash firmware/espnow_bridge to two ESP32s (Arduino-ESP32 core 3.x), then:
python examples/run_node.py --port /dev/ttyUSB0 --db alice.db --name alice --demo
python examples/run_node.py --port /dev/ttyUSB1 --db bob.db --name bob --demo
```

The mesh syncs the user app's database (`application/user_app/database.py`).
There's no separate mesh store. In the app: create
`Node(link, AppStore(Database(...)), clock=gps_time)` and call `node.poll()`
every few ms (or `run_forever()` in a thread). Write data through the app as
usual (`add_message`, `add_locale`, `set_location`); the node offers whatever
is new at the next encounter. `on_record` reports what peers sent, and
`on_delivered` reports that one of our messages reached its receiver.

## Protocol

**Frame header** (12 bytes, little-endian), on every frame:
`sender_id u32 | type u8 | flags u8 | session u16 | seq u32`.
The sender id comes first so the firmware can filter by partner (issue: "ESP-NOW packet filtering").

**Discovery.** All devices wake for `window` (2 s) every `period` (20 s) of
GPS-synced time, padded by `guard` for clock drift. Each sends a few BEACONs
(`version, last_server_sync, digest`) at random offsets inside the window.
If a peer's digest differs from ours, the **lower device id** sends HELLO and the
other answers HELLO_ACK. That is the handshake. The radio then stays on past
the window until the exchange ends.

**Exchange** (both sides do the same thing):
1. cutoff = min(both last server syncs)
2. INV: `(id, kind, created)` of every record this device created **or
   received** after the cutoff, except dead messages. So data picked up while
   offline still moves on.
3. WANT: ids from the peer's INV that I don't have (and no tombstone of mine has killed)
4. DATA: wanted records, split into chunks so every frame is ≤ 250 bytes
   (any record size, e.g. 1 MB = 4 425 frames), highest priority first
   (locale comments, messages, positions, tombstones), then DONE. Each arriving
   record goes to the app's `merge()`, which applies the app's rules.

Every INV/WANT/DATA/DONE frame is reliable (seq + ACK with a 32-bit selective-ack
bitmap, retransmit after `rto`, give up after `max_retries`). If no frame arrives
from the peer for `idle_timeout` seconds, the session ends. Records that fully
arrived are kept, partial ones are dropped, and the next encounter picks up the rest.

**Tombstones.** When a message reaches its receiver, the receiver's app
creates a `delivered` tombstone. It spreads like any record. Relays (anyone but
the sender and receiver) delete the message and keep passing the tombstone on.
The sender and receiver keep the message, marked dead, and the sender learns
it was delivered (`on_delivered`). Broadcast messages (`*`) are never
tombstoned. Messages and tombstones expire after 31 days. See
[docs/store.md](docs/store.md).

## Changes from the original design in issue #2

- **No TX/RX slot patterns.** ESP-NOW receives continuously whenever the radio
  is on and does listen-before-talk in hardware. Random beacon offsets inside a
  shared, time-synced window are enough. Slot scheduling may come back for LoRa.
- **Digest in beacons.** Peers holding identical sets skip the exchange entirely.
- **The data model is the app's.** Records are rows of the app's shared tables,
  and the app's `merge()`, `export()` and `purge()` decide what's kept. On top
  of that, the mesh deletes tombstoned messages on relays and counts *received*
  time in the cutoff. The app's own `/api/sync?since=` still counts only
  creation time.

## Toward LoRa

Everything above `Link` is radio-agnostic: frames are sized from `link.mtu`
(tests run at a 100-byte MTU), and receivers identify senders by the id in the
header, not by link address. A LoRa link needs `send` (broadcast-only is fine),
`poll`, `set_radio` and a much slower `Config` (longer `rto`, `idle_timeout`,
`hello_interval`, smaller `window_frames`).
