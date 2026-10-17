# espnow_serial.py: EspNowSerialLink

**Purpose.** The `Link` implementation for real hardware. It talks over USB
to an ESP32 running `firmware/espnow_bridge` ([firmware.md](firmware.md)).
How the bytes travel is a **transport** ([transport.md](transport.md)): pyserial
on laptops, `termux-usb` on Android. This module only speaks the bridge protocol
below and is identical for every transport.

## Host ↔ ESP32 serial protocol

Every message is wrapped in **SLIP** framing, so message boundaries survive a
byte stream:
- Each message starts and ends with `0xC0`.
- A `0xC0` byte inside a message is sent as `0xDB 0xDC`.
- A `0xDB` byte inside a message is sent as `0xDB 0xDD`.

The first byte of each message is the command or event code:

| Direction | Code | Name | Body |
|---|---|---|---|
| host → ESP32 | 0x01 | SEND | `mac[6]` + frame (`ff:ff:ff:ff:ff:ff` = broadcast) |
| host → ESP32 | 0x02 | SET_PARTNER | `device_id u32` little-endian (0 = accept all) |
| host → ESP32 | 0x03 | RADIO | `u8` 1 = on, 0 = off |
| host → ESP32 | 0x04 | GET_INFO | none |
| ESP32 → host | 0x81 | RECV | `mac[6]` + `rssi i8` + frame |
| ESP32 → host | 0x84 | INFO | own `mac[6]` + status block (see [firmware.md](firmware.md)) |
| ESP32 → host | 0x85 | LOG | UTF-8 text |

## Threads and data flow

```
reader thread:  transport.read(512) -> SlipDecoder.feed -> per message:
                  RECV  -> RxFrame onto a queue
                  INFO  -> self.mac = ..., self.status = {...}
                  LOG   -> log("[esp32]", text)
main thread:    Node.poll() -> link.poll() empties the queue without blocking
                Node -> link.send/set_radio/set_partner -> SLIP-encode -> transport.write
```

- Every transport's `read` returns after at most ~50 ms without data, so the
  reader thread checks its stop flag at least every 50 ms.
- Writes happen on the caller's thread. pyserial writes block until the bytes
  are queued to the OS; the Android transport blocks until the board takes them
  (at most 2 s, then the board counts as lost).

## API

| Call | Behaviour |
|---|---|
| `EspNowSerialLink(port="auto", baud=460800, log=print)` | Opens the transport named by `port` (see [transport.md](transport.md): `auto`, `COM3`, `/dev/ttyACM0`, `socket://127.0.0.1:5000`, `termux-usb`, ...), starts the reader thread, sends GET_INFO. Raises `TransportError` if the board can't be opened. On Android, if the program wasn't started by termux-usb, it reruns the program under termux-usb and exits |
| `.mac` | The board's own MAC, set once the INFO reply arrives (None until then) |
| `.status` | Dict from the latest INFO: `radio_on, channel, last_err, tx_ok, tx_err, rx, rx_filtered, rx_dropped`. None until a reply arrives, or if the firmware is older. |
| `request_status()` | Sends GET_INFO; `.mac` and `.status` update when the reply arrives |
| `send(payload, dest)` | Raises `ValueError` if the frame is over 250 bytes |
| `poll()` | Empties the receive queue. Raises `LinkLost` once the board is gone for good (Android only, see below) |
| `set_radio(on)`, `set_partner(id)` | One command each |
| `close()` | Stops the reader and closes the transport |
| `.connected` | False while the board is unplugged |
| `.lost` | True once the board is unplugged and the transport can't reopen it (Android) |

`slip_encode` and `SlipDecoder` are plain functions/classes with no state
shared between instances. The test suite uses them directly.

## Assumptions

- **The board uses its native USB (ESP32-S3).** USB CDC ignores the baud rate,
  so `baud` and the firmware's `BAUD` no longer matter; the link still sends
  460800 for boards with a USB-UART chip.
- **There's one host process per board.** Nothing else (Arduino Serial
  Monitor, another script) can have the port open.
- **The first GET_INFO can be lost** if the board is still booting (just
  plugged in), and `.mac` then stays None until the next `request_status()`.
  Nothing in the protocol uses `.mac`; it's only informational.
- **Commands are fire-and-forget.** There's no reply to SEND or SET_PARTNER.
  The protocol's own ACKs cover lost radio frames. Errors surface through
  firmware LOG lines and the status counters.
- **The board keeps state across host restarts** (partner filter, radio on/off).
  That's why the constructor resets both.

## Unplug and replug

The board can disappear at any moment (cable bumped, board reset). The link
handles this without crashing the node:

1. A read or write error closes the port and sets `connected = False`. It logs
   `[link] lost connection ...`.
2. While disconnected, `send`/`set_radio`/`set_partner` don't raise. Frames are
   dropped (counted in `dropped_writes`), and the protocol treats them like any
   other radio loss: retransmits, or a session timeout.
3. The reader thread tries to reopen the **same port** every
   `reconnect_interval` seconds. Use `auto` or the `/dev/serial/by-id/...` path,
   because `ttyACM` numbers can change on replug.
4. On reconnect it re-sends the **desired** state (partner filter, radio on/off),
   because a replugged board has rebooted and forgotten both. It logs `[link] reconnected ...`.

**Android is different.** Android grants USB access only to a newly started
program, so the running one can't reopen the board. After step 1 the reader
thread stops, `.lost` becomes True, and `poll()` raises `LinkLost` once the
frames already received are delivered. `run_node.py` exits with a message;
start it again to reconnect.

Unplugging is **not** how to simulate a hiker walking away: that leaves the
host without a radio. Use `bulk_test.py --cut-after/--cut-for`, which switches
the radio off while USB stays up.

## Known gaps

- Neither side checks that serial messages arrived intact. USB serial is
  reliable in practice, and a corrupted frame would either fail to decode or
  fail the record id check.
- Sending is not flow-controlled. If the host writes faster than the ESP32 can
  transmit, the firmware's serial buffer (4 KB) fills, and its retry loop in
  `radioSend` delays reads. The session's 16-frame window keeps bursts small
  (16 × ~260 B ≈ 4 KB).
