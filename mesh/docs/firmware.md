# espnow_bridge.ino: ESP32 bridge firmware

**Purpose.** Turns an ESP32 into a USB ESP-NOW radio for the Python host. It
has **no protocol logic**. It forwards frames in both directions and applies
the partner filter from issue #2 ("ESP-NOW packet filtering").

> **Status: never compiled.** There was no Arduino toolchain where it was
> written. It targets **Arduino-ESP32 core 3.x** (ESP-IDF 5.x).

## Constants

| Name | Value | Notes |
|---|---|---|
| `CHANNEL` | 1 | **All devices must use the same Wi-Fi channel** |
| `BAUD` | 460800 | Must match `EspNowSerialLink` |
| Receive queue | 64 items | Frames are dropped if the host falls behind |

## Control flow

```
setup():
    serial with 8 KB receive buffer, at BAUD
    create the receive queue
    radioStart()
    send LOG "espnow_bridge ready"

loop(), forever:
    readHost(): SLIP-decode serial bytes -> handleCommand() for each complete message
    drain the receive queue -> send each frame to the host as RECV (mac, rssi, frame)

onRecv() (runs in the Wi-Fi task, must be fast):
    drop the frame if it is shorter than 4 bytes
    sender = first 4 bytes (u32 little-endian)
    if partner != 0 and sender != partner: drop       # <- the partner filter
    copy the frame into the queue (don't wait if it's full)
```

## Commands

| Command | Action |
|---|---|
| SEND | `ensurePeer(mac)`, then `esp_now_send`. If the ESP-NOW transmit queue is full (`ESP_ERR_ESPNOW_NO_MEM`), retry every 1 ms, up to 20 times. Other errors are ignored silently. |
| SET_PARTNER | `partner = id` (0 = accept everyone) |
| RADIO | on → `radioStart()`, off → `radioStop()` |
| GET_INFO | Reply INFO with the board's own MAC |

## Radio power

- `radioStart()`: Wi-Fi in station mode, set the channel, `esp_now_init`,
  register the receive callback, add the broadcast peer.
- `radioStop()`: `esp_now_deinit`, then `WiFi.mode(WIFI_OFF)`. This clears the
  peer table, so peers are added again on the next send.

The Python Node calls these at the start and end of each discovery window,
about twice every 20 s with the defaults.

## Peer table

ESP-NOW only sends unicast frames to registered peers, and the table holds
about 20. `ensurePeer` adds a peer the first time a MAC is used. If the table
is full, it deletes the first unicast peer it finds (not least-recently-used)
and tries again.

## The partner filter, and why the device id comes first

The filter reads only bytes 0–3, so it works without understanding frame
types. While a session is active, the firmware drops **everything** from other
devices, beacons included. That matches the Node, which ignores beacons while
busy anyway.

## Assumptions

- **Arduino-ESP32 core 3.x.** On 2.x, the receive callback signature is
  different: `(const uint8_t *mac, const uint8_t *data, int len)`, with no RSSI.
- **ESP-NOW v1 frame size (250 bytes)**, which works on every version.
- **Unicast frames get the hardware ACK and retries for free**, but the host
  never finds out whether a send succeeded (there's no send callback). The
  protocol's own ACKs handle loss.
- **The board isn't joined to a Wi-Fi network.** Joining an access point would
  move the radio to that network's channel and break the mesh.

## Things to verify on first flash

1. It compiles on your core version.
2. After a reset, "espnow_bridge ready" shows up in the host log.
3. Two boards running `examples/run_node.py --demo` complete a session.
4. Toggling the radio every 20 s (`run_node.py` without `--demo`) doesn't
   cause errors or drop the USB connection.
5. A 200 KB record transfers without RECV frames being dropped. If frames are
   lost, increase the queue size or lower `window_frames` on the host.
