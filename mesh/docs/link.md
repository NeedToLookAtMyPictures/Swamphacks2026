# link.py: the radio interface

**Purpose.** The boundary between the protocol and a specific radio. Everything
above it (Node, Session, wire format) is radio-agnostic. To add LoRa, write one
new class that implements this interface.

## `RxFrame`

```python
RxFrame(payload: bytes, src: bytes | None, rssi: int | None = None)
```
- `src` is the link-layer address: a 6-byte MAC for ESP-NOW, or `None` if the
  radio has no addressing.
- `rssi` is signal strength in dBm. Nothing uses it yet. It could be used to
  prefer close peers, or to estimate distance.

## `Link` (abstract base class)

| Member | Required? | Contract |
|---|---|---|
| `mtu: int` | yes | Max frame size **including** the 12-byte header. Default 250. |
| `send(payload, dest=None)` | yes | Send one frame. `dest=None` means broadcast. Must not block for long. |
| `poll()` | yes | Return all frames received since the last call. **Must not block.** |
| `set_radio(on)` | optional | Power the radio on or off for duty cycling. Default: does nothing. |
| `set_partner(device_id or None)` | optional | Ask the hardware to drop frames not from this device id. Default: does nothing. The Node filters in software anyway. |
| `close()` | optional | Release resources |

## How the Node uses it

- Beacons are sent with `send(frame, None)` (broadcast).
- Session frames are sent with `send(frame, peer_mac)` if the peer's address is
  known, otherwise broadcast.
- Peers are identified by the **device id in the frame header**, never by
  `src`. `src` is only remembered so unicast can be used.

## Implementations

| Class | File | Notes |
|---|---|---|
| `EspNowSerialLink` | [espnow_serial.md](espnow_serial.md) | Real hardware over USB |
| `SimLink` | [sim.md](sim.md) | Tests and demos |
| LoRa link | not written | See below |

## What a LoRa link needs

- `mtu` around 200 (an SX126x radio carries up to 255 bytes; smaller frames have shorter airtime).
- `send` can always broadcast and ignore `dest`. LoRa has no MAC addressing,
  and the device id in the header already does the filtering.
- `set_partner` is optional. Filtering on the host is fine at LoRa's low data rates.
- A slow `Config`: `rto` of several seconds, `window_frames` of 1–4, longer
  `idle_timeout` and `hello_interval`, and probably delayed ACKs (see
  [reliable.md](reliable.md)).
