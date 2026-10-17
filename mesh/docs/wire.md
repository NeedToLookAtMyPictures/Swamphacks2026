# wire.py: frame format

**Purpose.** Byte layout of every frame sent over the radio. It doesn't depend
on the link, so the same frames work over ESP-NOW and later LoRa.

## Header (12 bytes, little-endian, on every frame)

```
offset 0   sender   u32   device id of the sender
offset 4   type     u8    message type (table below)
offset 5   flags    u8    bit 0 = LAST (final frame of an INV or WANT list)
offset 6   session  u16   session id, chosen at random by the initiator (0 for beacons)
offset 8   seq      u32   sequence number for reliable frames (0 otherwise)
offset 12  payload  ...   up to mtu - 12 bytes
```

**Why the sender id comes first:** the firmware decides whether to forward a
frame by reading bytes 0–3 and comparing them with the current partner. It
doesn't need to understand anything else in the frame.

**Differs from issue #2:** the issue writes the handshake as
`[HANDSHAKE_COMMAND][DEVICE_ID]`. Here every frame, handshake included, is
`[DEVICE_ID][TYPE]...`. Same information, opposite order, and one filter rule
covers every frame.

## Message types

| Type | Value | Reliable? | Payload | Sent by |
|---|---|---|---|---|
| BEACON | 1 | no (broadcast) | `version u8, last_server_sync u32, digest 8B` | Node, during windows |
| HELLO | 2 | no (retried by session) | `version u8, last_server_sync u32` | Initiator |
| HELLO_ACK | 3 | no (re-sent if HELLO repeats) | `version u8, last_server_sync u32` | Responder |
| ACK | 4 | no | `next_expected u32, sack_bitmap u32` | Both, for every reliable frame received |
| INV | 5 | **yes** | repeated `id 8B, kind u8, created u32` (13 B each) | Both |
| WANT | 6 | **yes** | repeated `id 8B` | Both |
| DATA | 7 | **yes** | `record_id 8B, chunk_index u16, chunk_count u16, chunk bytes` | Both |
| DONE | 8 | **yes** | empty | Both |

`RELIABLE = {INV, WANT, DATA, DONE}`. These get sequence numbers, ACKs and
retransmission (see [reliable.md](reliable.md)).

## Size limit: no frame over 250 bytes

ESP-NOW carries at most 250 bytes per packet, so `MAX_FRAME = 250`. The
session caps its frame size at `min(link.mtu, MAX_FRAME)`. Even a link that
reports a bigger MTU never gets a frame over 250 bytes, header included.

Records themselves can be any size. A record bigger than one frame is split
into chunks, each sent as its own DATA frame:

```
DATA frame = 12 B header | record_id 8B | chunk_index u16 | chunk_count u16 | up to 226 B of the blob
blob       = CRC32 of the encoded record (u32) | encoded record
```

A 1 MB record becomes 4 425 frames of at most 250 bytes. Two sequence numbers
make the reassembly lossless:
- the **frame `seq`** (reliable layer) detects lost frames, triggers
  retransmission and hands frames to the session in order
  ([reliable.md](reliable.md));
- the **`chunk_index`** places each chunk in its record. The record is rebuilt
  only once all `chunk_count` chunks are present, then its CRC32 and its id
  are checked ([session.md](session.md)).

## Capacity per frame (ESP-NOW, mtu = 250)

| Item | Per frame |
|---|---|
| INV entries | (250 − 12) / 13 = **18** |
| WANT ids | (250 − 12) / 8 = **29** |
| DATA chunk bytes | 250 − 12 − 12 = **226** |

With a smaller LoRa MTU, all of these shrink automatically, because
`session.py` computes them from `link.mtu`.

Limits that follow from the field sizes:
- A record can have at most 65 535 chunks (`MAX_CHUNKS`). At 226 B per chunk
  that's about 14.8 MB per record. The sender skips a larger record rather
  than crash.
- `seq` is a u32, so it will never wrap within a session.

## `Frame` API

- `Frame(sender, type, flags=0, session=0, seq=0, payload=b"")`
- `frame.encode()` returns bytes.
- `Frame.decode(data)` returns a `Frame`, or **None** if the frame is shorter
  than 12 bytes or the type byte is unknown. That's how stray 2.4 GHz traffic
  gets ignored.

## Versioning

`PROTOCOL_VERSION = 2` appears only in BEACON and HELLO/HELLO_ACK. Version 2
changed the records to mirror the app's tables and added the CRC32 to DATA. Nodes ignore
beacons and HELLOs with a different version, so devices on mismatched versions
just never pair. The version check covers the HELLO a responder receives, but
the initiator doesn't check the version in HELLO_ACK.

## Assumptions and gaps

- Nothing marks a frame as belonging to this protocol (no "magic" bytes). A
  foreign ESP-NOW packet that is at least 12 bytes long, with a byte 4 between
  1 and 8, decodes as a frame. In practice it gets dropped anyway: beacons and
  HELLOs need a matching version byte and a minimum length, and other types
  need a matching sender and session id.
- There's no checksum per frame. ESP-NOW already discards corrupted frames
  using the 802.11 frame check (FCS), and each reassembled record carries a
  CRC32. LoRa also has a CRC that should be left on.
- `last_server_sync` in BEACON and HELLO is the app's REAL value rounded down
  to whole seconds (u32).
