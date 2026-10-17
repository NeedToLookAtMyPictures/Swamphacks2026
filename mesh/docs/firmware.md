# espnow_bridge.ino: ESP32 bridge firmware

**Purpose.** Turns an ESP32 into a USB ESP-NOW radio for the Python host. It
has **no protocol logic**. It forwards frames in both directions and applies
the partner filter from issue #2 ("ESP-NOW packet filtering").

> **Status:** the radio startup sequence now copies the team's working
> `esp-now-modem` sketch (wait for `WiFi.STA.started()`, channel 6, check
> every error). The full bridge has still not been compiled here. It requires
> **Arduino-ESP32 core 3.x** (ESP-IDF 5.x).

## Constants

| Name | Value | Notes |
|---|---|---|
| `CHANNEL` | 6 | **All devices must use the same Wi-Fi channel** (6 matches esp-now-modem) |
| `BAUD` | 460800 | Ignored on the ESP32-S3's native USB (Feather S3). Real on boards with a USB-UART chip (Heltec V3's CP2102): the host must use the same rate, which it does by default. |
| Receive queue | 32 items | Frames are dropped (and counted) if the host falls behind |

## Control flow

```
setup():
    serial with 4 KB receive buffer, at BAUD
    create the receive queue
    WiFi OFF. The radio starts only when the host sends RADIO on, so any
    startup error is logged while the host is listening.
    send LOG "espnow_bridge ready"

loop(), forever:
    readHost(): SLIP-decode serial bytes -> handleCommand() for each complete message
    drain the receive queue -> send each frame to the host as RECV (mac, rssi, frame)

onRecv() (runs in the Wi-Fi task, must be fast):
    drop the frame if it is shorter than 4 bytes
    sender = first 4 bytes (u32 little-endian)
    count it (rx)
    if partner != 0 and sender != partner: count (rx_filtered), drop   # <- the partner filter
    copy the frame into the queue; if it's full, count (rx_dropped)
```

## Commands

| Command | Action |
|---|---|
| SEND | `ensurePeer(mac)`, then `esp_now_send`. If the ESP-NOW transmit queue is full (`ESP_ERR_ESPNOW_NO_MEM`), retry every 1 ms, up to 20 times. Each send counts as `tx_ok` or `tx_err`. Failures are logged to the host on the 1st, 2nd, 4th, 8th... occurrence, with the ESP-IDF error name. |
| SET_PARTNER | `partner = id` (0 = accept everyone) |
| RADIO | on → `radioStart()`, off → `radioStop()` |
| GET_INFO | Reply INFO: own MAC (`esp_read_mac`, works with Wi-Fi off) + status block |

### Status block (in the INFO reply)

`radio_on u8, channel u8, last_err i32, tx_ok u32, tx_err u32, rx u32, rx_filtered u32, rx_dropped u32`
(packed, little-endian; must match `STATUS` in `espnow_serial.py`). `last_err` is
the `esp_err_t` of the most recent failure. The firmware log line gives its name.

### Text commands (for a human at a serial monitor)

Type a command and press Enter, at any baud rate (native USB ignores it):

| Command | Reply |
|---|---|
| `HELP` | List of commands |
| `ID` | `OK espnow_bridge mac=b8:f8:62:d6:67:0c radio=on channel=6` |
| `STATUS` | Radio state, partner filter, sent / send errors / received / filtered / dropped counts, last error name |
| `ON` / `OFF` | Start or stop the radio, like esp-now-modem |
| `TX <text>` | Broadcast raw text. The other board prints it if it's also in text mode. |

The firmware has **no device id**. The trailmesh device id lives in Python
(`--id`), so `ID` reports the MAC address instead.

How the two formats coexist: Python's binary messages always start with
`0xC0`, which never appears in typed text. Bytes outside a binary message are
collected into a line and run as a text command. A text command switches the
board into **text mode**: logs print as `LOG ...` lines and received frames as
`RX <mac> <rssi> <printable payload>` (non-printable bytes become `.`, like
esp-now-modem). The next binary message from Python switches back. If a binary
message stops partway (host killed mid-write), it's dropped after 500 ms, so
typed commands keep working.

## Radio power

- `radioStart()`: Wi-Fi in station mode, **wait up to 3 s for `WiFi.STA.started()`**,
  set the channel, `esp_now_init`, register the receive callback, add the
  broadcast peer. Any failure is logged to the host, stored in `last_err`, and
  leaves `radio_on = 0`. The host retries on its next RADIO on.
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

## Why the first version didn't work (most likely)

The first version called `esp_wifi_set_channel` and `esp_now_init` right after
`WiFi.mode(WIFI_STA)`, without waiting for the interface to finish starting,
and it ignored their return codes. Its only error message was sent during
`setup()`, before the host was connected, so a failure was invisible. The
working esp-now-modem sketch waits for `WiFi.STA.started()` and checks every
call. That wait is the most likely fix, but without the board's error output
from the failed runs, this is a deduction, not a confirmed diagnosis. If it
still fails, the new status block and logs will show the exact error.

## Flashing the Adafruit Feather ESP32-S3

Board id: `esp32:esp32:adafruit_feather_esp32s3` (defaults are correct: USB CDC On Boot = Enabled).

- **The USB name changes with the mode.** While running firmware, the board is
  `/dev/serial/by-id/usb-Adafruit_Feather_ESP32-S3_<mac>-if00`. In the
  bootloader it's `usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00`.
  Uploading switches modes, so a by-id path doesn't survive the upload.
  Use the plain port (`/dev/ttyACM0`), or the Espressif path if the board is
  already in the bootloader.
- **After an upload, the board can stay stuck in the bootloader.** Its
  "Hard resetting via RTS" step didn't restart our boards. A watchdog reset does:
  ```bash
  ~/.arduino15/packages/esp32/tools/esptool_py/5.3.1/esptool --chip esp32s3 --port /dev/ttyACM0 --after watchdog-reset chip-id
  ```
  Unplugging and replugging the board also works.

Tested on two Feather S3s, with core 3.3.12, on 2026-10-17:
- `diagnose.py`: 10/10 pings each way, about -37 dBm at close range, 0 errors.
- `run_node.py --demo`: a locale update synced in one session.
- A 50 KB image arrived intact in 4.1 s, discovery included (about 12 KB/s). There were
  0 send errors, 0 dropped frames, and 295 frames in each direction.

## Flashing a generic ESP32-S3FN8 board

ESP32-S3FN8 is the chip (8 MB flash inside, no PSRAM). With a board that has no
board definition of its own, use **ESP32S3 Dev Module** and set:

- **USB CDC On Boot: Enabled.** Required. The default on this board definition
  is Disabled, which sends `Serial` to the UART pins instead of the USB port,
  and the host then hears nothing.
- **USB Mode:** either works. Both present the same USB CDC interface to the
  host (laptops and Android).
- **Flash Size: 8MB**, PSRAM: Disabled.

If the board has two USB ports, plug into the one wired to the chip's own USB
(usually labeled USB, not UART/COM).

## Flashing the Heltec WiFi LoRa 32 V3

ESP32-S3 with an SX1262 LoRa radio (not used by this firmware: the bridge is
ESP-NOW over the ESP32's Wi-Fi radio). Its USB-C port is **not** the chip's
native USB: it goes through a **Silicon Labs CP2102** USB-UART chip, so the
host sees `/dev/ttyUSB0` (Linux), a "CP210x" COM port (Windows, may need the
Silicon Labs driver), and on Android the Termux transport's CP210x driver.

Board: **Heltec WiFi LoRa 32(V3)** (from the Heltec or the stock esp32 board
package), or **ESP32S3 Dev Module**. Either way:

- **USB CDC On Boot: Disabled.** Required, the opposite of the generic board
  above. `Serial` must go to UART0, which is wired to the CP2102. With it
  Enabled, `Serial` goes to native USB pins that aren't connected to the port,
  and the host hears nothing.
- Flash Size: 8MB.
- Boards often ship with Meshtastic or Heltec's factory demo installed, so
  flash the bridge before trying the host.
- Uploading resets the board automatically (the CP2102's DTR/RTS drive EN and
  IO0). If an upload can't connect, hold PRG, tap RST, release PRG, and retry.

## Things to verify on first flash

1. It compiles on your core version.
2. `python examples/diagnose.py --port ...` reports "radio on, channel 6" on both boards.
3. Two boards running `examples/run_node.py --demo` complete a session.
4. Toggling the radio every 20 s (`run_node.py` without `--demo`) doesn't
   cause errors or drop the USB connection.
5. A 200 KB record transfers without RECV frames being dropped. If frames are
   lost, increase the queue size or lower `window_frames` on the host.
