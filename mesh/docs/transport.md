# transport.py and termux_usb.py: how bytes reach the board

**Purpose.** `EspNowSerialLink` speaks the bridge protocol (SLIP-framed
commands, see [espnow_serial.md](espnow_serial.md)). A **transport** is the
byte pipe underneath it. The link, the Node and everything above never know
which transport is in use.

| Transport | Where | How |
|---|---|---|
| `PySerialTransport` (transport.py) | Windows, macOS, Linux | Opens the board's USB serial port with pyserial (`COM3`, `/dev/ttyACM0`, `/dev/ttyUSB0`, `/dev/cu.usbmodem...`). Also pyserial URLs such as `socket://127.0.0.1:5000`, used by the tests. |
| `TermuxUsbTransport` (termux_usb.py) | Android, in Termux | Android apps can't open serial ports. `termux-usb` hands the program a USB file descriptor; this transport drives the board's USB serial interface through libusb. |

Both talk to the **same USB interface** on the board, so the firmware is the
same everywhere. Two kinds of USB serial port are supported:

| Board | USB serial | Notes |
|---|---|---|
| Adafruit Feather ESP32-S3, generic ESP32-S3FN8 | USB CDC (the chip's native USB, standard class) | Baud rate ignored |
| Heltec WiFi LoRa 32 V3 | Silicon Labs CP2102 USB-UART chip (vendor 0x10C4, its own protocol) | Baud rate is real: host and firmware both use 460800. Firmware must be built with USB CDC On Boot **Disabled** (see [firmware.md](firmware.md)). |

Other USB-UART chips (CH340, FTDI) aren't implemented on Android; on a laptop
pyserial handles them, but `--port auto` won't find them.

## Choosing a transport: the port string

| `port` | Transport |
|---|---|
| `auto` (default) | Android: Termux, the one USB device plugged in. Elsewhere: pyserial, the one port whose USB vendor is Espressif (0x303A), Adafruit (0x239A) or Silicon Labs (0x10C4, CP210x). |
| `termux-usb` | Termux, the one USB device plugged in |
| `/dev/bus/usb/001/002` | Termux, that device (from `termux-usb -l`) |
| anything else | pyserial |

`auto` fails with a message listing the candidates if there are several.

## The Transport interface

```
read(size) -> bytes   up to size bytes, or b"" after ~50 ms with no data; TransportError once broken
write(data)           all of data, or TransportError
close()               never raises; may be called twice, from any thread
reconnectable         False if the running program can't reopen it after it breaks
```

A read on one thread and a write on another must be safe at the same time.

**Adding a transport** (another radio modem, Bluetooth, ...): subclass
`Transport` and call `transport.register(matches, opener)` once at startup,
where `matches(port) -> bool` claims port strings and `opener(port, baud)`
returns the transport. Registered transports are tried before the built-in ones.
Nothing in the link or the protocol changes.

## Android (Termux)

**One-time setup:**

1. Install the Termux:API app from the **same place as Termux** (F-Droid or
   GitHub). The Play Store builds are signed differently and don't work together.
2. `pkg install python termux-api libusb`. pyserial isn't needed.
3. Plug the board into the phone (USB-C, or an OTG adapter).

**Running:** the same command as on a laptop, e.g.
`python examples/run_node.py --name alice --demo`. What happens:

1. `--port auto` picks the Termux transport.
2. The program wasn't started by termux-usb, so the transport runs
   `termux-usb -l`, then reruns the exact same command as
   `termux-usb -r -E -e "<command>" <device>`. Android may show the "allow
   access" dialog. The first process just waits, and exits with the rerun's
   exit code. Ctrl-c goes to the rerun.
3. In the rerun, `TERMUX_USB_FD` holds the device's file descriptor. The
   transport wraps it with libusb (with device discovery off, since that
   needs root) and reads the device and configuration descriptors.
   - **CDC:** claims the CDC interfaces, clears the endpoints, and turns DTR on (RTS off).
   - **CP210x** (vendor 0x10C4, no CDC interface): claims its one vendor
     interface, enables the UART, sets the baud rate and 8N1, and turns DTR and
     RTS **off**. On the Heltec V3, DTR/RTS drive the ESP32's EN and IO0 pins
     (auto-flash circuit); with both off the board runs normally.

**Limits:**

- **One board per program.** termux-usb grants one device per program start.
  `bulk_test.py` (two boards in one process) is laptop-only.
- **No reconnecting.** Android grants USB access to newly started programs only.
  If the board is unplugged, the link reports it lost (`LinkLost`) and
  `run_node.py` exits. Start it again.
- **A stalled board counts as lost.** If the board takes no data for 2 s, the
  write fails and the link is lost. A half-written command can't be cancelled
  safely, so it isn't retried.
- With several USB devices plugged in, pass the board's `/dev/bus/usb/...` path:
  Android won't reveal which device is which before permission is granted.
- termux-usb runs the command through its callback. Arguments with spaces are
  quoted, but this hasn't been tested on every Termux version: avoid spaces in
  paths if a rerun misbehaves.

**Not done:** neither USB CDC nor CP210x on Android has run on a phone yet. The descriptor parsing,
the rerun and the link's lost-board handling are unit-tested
(`tests/test_transport.py`), and the libusb calls were checked against a real
libusb on Linux, but not with a board attached.
