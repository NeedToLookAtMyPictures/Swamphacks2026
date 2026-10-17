# trailmesh: gossip sync over ESP-NOW

Python implementation of the device-to-device protocol from issue #2 (ESP-NOW
part). The ESP32 is a thin radio bridge; all protocol logic runs in Python.

```
mesh/
  trailmesh/         protocol package (pure Python, stdlib only)
    records.py       Record model: content-hashed ids, kinds, priorities, supersede keys
    store.py         SQLite store: inventory under cutoff, digest, supersede + tombstones
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
pip install pyserial
# flash firmware/espnow_bridge to two ESP32s (Arduino-ESP32 core 3.x), then:
python examples/run_node.py --port /dev/ttyUSB0 --id 1 --demo --post "spring at mile 1203 is dry"
python examples/run_node.py --port /dev/ttyUSB1 --id 2 --demo
```

In your app: create a `Node(device_id, link, RecordStore("trail.db"), clock=gps_time)`,
call `node.poll()` every few ms (or `run_forever()` in a thread), add data with
`node.post(Record...)`, and get notified of incoming data via `on_record`.

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
2. INV: `(id, kind, created)` of every record eligible under the cutoff
3. WANT: ids from the peer's INV that I don't have
4. DATA: wanted records, chunked to fit the MTU, highest priority first
   (locale updates, messages, pins, images, maps), then DONE

Every INV/WANT/DATA/DONE frame is reliable (seq + ACK with a 32-bit selective-ack
bitmap, retransmit after `rto`, give up after `max_retries`). If no frame arrives
from the peer for `idle_timeout` seconds, the session ends. Records that fully
arrived are kept, partial ones are dropped, and the next encounter picks up the rest.

## Changes from the original design in issue #2

- **No TX/RX slot patterns.** ESP-NOW receives continuously whenever the radio
  is on and does listen-before-talk in hardware. Random beacon offsets inside a
  shared, time-synced window are enough. Slot scheduling may come back for LoRa.
- **The cutoff compares against `server_time`, not creation time.** A record
  written offline before the cutoff that reached the server *after* it must still
  be exchanged. Each record carries the time the server first saw it (0 = never),
  and only records the server had at or before the cutoff are skipped.
- **Digest in beacons.** Peers holding identical sets skip the exchange entirely.
- **Overrides.** Records with a supersede key (locale updates use `locale:field`)
  replace older ones with the same key. Replaced ids are tombstoned so they are
  never requested again. Messages and pins have no key, so all of them are kept
  (pins match the issue's "store every pin" rule).
- **Location sharing.** On each handshake the node adds a fresh pin of its own
  position (if `position` is provided); pins then gossip like any other record.

## Toward LoRa

Everything above `Link` is radio-agnostic: frames are sized from `link.mtu`
(tests run at a 100-byte MTU), and receivers identify senders by the id in the
header, not by link address. A LoRa link needs `send` (broadcast-only is fine),
`poll`, `set_radio` and a much slower `Config` (longer `rto`, `idle_timeout`,
`hello_interval`, smaller `window_frames`).
