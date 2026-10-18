# Swamphacks-2026: MiRIAD

Hikers often have no cell service. This project lets their devices pass
messages, trail comments ("spring at mile 12 is dry") and last-known
positions from hiker to hiker, with no internet. Whenever a device does
reach the internet, it syncs with a server.

There are three parts:

| Part | Folder | What it is |
|---|---|---|
| **User app** | `application/user_app/` | A small web app (Python + Flask) with an offline map, messages, comments and positions. You open it in a browser. |
| **Mesh** | `mesh/` | The Python code (`trailmesh`) that syncs the app's data between devices over radio, plus the firmware for the radio boards. |
| **Server** | `application/server/` | Optional. Collects everyone's data when a device has internet or shares its wifi. |

The radio boards don't make any decisions. Each one is plugged into a laptop,
Raspberry Pi or phone over USB and just forwards radio packets. All the logic
runs in Python on that computer.

```
 browser ──> user app (Flask) ──> app database <── trailmesh node ──USB──> radio board ~~radio~~> other hikers
                    └──────── internet / wifi ────────> server (optional)
```

## 1. Hardware

You need **two boards** of the same kind to see anything sync. Each board needs
its own computer, or both can share one computer (see section 5).

| Board | Radios | Firmware | Notes |
|---|---|---|---|
| **Heltec WiFi LoRa 32 V3**, or a clone (ESP32-S3 + SX1262, 863–928 MHz). Sold in 2-packs as "LoRa V3 ESP32 ... SX1262". | LoRa (miles) **and** ESP-NOW (about 60 m) | `mesh/firmware/heltec_bridge` | Recommended. **Attach the LoRa antenna before powering it**: transmitting without one can damage the radio chip. |
| Adafruit Feather ESP32-S3, or any ESP32-S3 board | ESP-NOW only | `mesh/firmware/espnow_bridge` | Short range only. |

Also needed:
- **A USB-C data cable** for each board. Many cables only charge; if the board
  never shows up on the computer, try another cable.
- **A host computer** for each board: Windows, macOS or Linux with Python
  3.10+. A Raspberry Pi, or an Android phone running Termux, also works.

The LoRa defaults (915 MHz, 500 kHz bandwidth) are for the US. Elsewhere, check
your local rules before transmitting.

## 2. Install the software

### Python (every host computer)

Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/).
On Windows, tick "Add python.exe to PATH" during install. Then, from the repo folder:

```bash
pip install -r application/user_app/requirements.txt   # Flask, pmtiles (the app)
pip install -r mesh/requirements.txt                    # pyserial (talks to the board over USB)
pip install -r application/server/requirements.txt      # only on the machine that runs the server
```

### Offline map (once, needs internet)

```bash
cd application/user_app
python download_locales.py map      # downloads the Gainesville-Hawthorne, FL map to data/map.pmtiles
```

You can skip this if the device will reach a server: the app downloads the
server's map during sync. For other areas, see `application/user_app/README.md`.

### Arduino IDE (only to flash boards)

1. Install **Arduino IDE 2** from [arduino.cc/en/software](https://www.arduino.cc/en/software).
2. Add ESP32 support: **File → Preferences → Additional boards manager URLs**, add
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`, then
   **Tools → Board → Boards Manager**, search **esp32** (by Espressif Systems)
   and install **version 3.x** (tested with 3.3.12). Version 2.x will not compile.
3. Install the LoRa driver (Heltec boards only): **Tools → Manage Libraries**,
   search **RadioLib** (by Jan Gromeš) and install **version 7.x** (tested with 7.8.1).
4. **Windows only: CP210x driver (Heltec boards only).** The Heltec talks to the
   computer through a Silicon Labs CP2102 chip. If the board doesn't appear as
   "Silicon Labs CP210x USB to UART Bridge (COMx)" under Device Manager → Ports,
   install the "CP210x Universal Windows Driver" from Silicon Labs, then replug.

## 3. Flash the boards

Do this once per board, and again whenever the firmware changes.

### Heltec WiFi LoRa 32 V3

1. Screw on the LoRa antenna, then plug the board in.
2. In Arduino IDE: **File → Open** `mesh/firmware/heltec_bridge/heltec_bridge.ino`.
3. **Tools → Board → esp32 → Heltec WiFi LoRa 32(V3)**. The default settings are correct.
4. **Tools → Port**: the board's port. On Windows it's a COM port (not the
   "Bluetooth" ones); on Linux `/dev/ttyUSB0`; on macOS `/dev/cu.usbserial-...`.
5. Click **Upload** (→). If the upload doesn't start, hold the board's **PRG**
   button while you click Upload, and let go once it says "Connecting...".

From the command line instead (arduino-cli is bundled with Arduino IDE 2):

```bash
arduino-cli compile --upload -p COM5 --fqbn esp32:esp32:heltec_wifi_lora_32_V3 mesh/firmware/heltec_bridge
```

On Windows the bundled copy is at
`%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`.

### Adafruit Feather ESP32-S3 / other ESP32-S3 (ESP-NOW only)

Open `mesh/firmware/espnow_bridge/espnow_bridge.ino`, then:
- **Feather:** board **Adafruit Feather ESP32-S3** (defaults are correct).
- **Other ESP32-S3 boards:** board **ESP32S3 Dev Module**, and set
  **Tools → USB CDC On Boot → Enabled** (required, or the computer hears nothing).

Then Upload. If the board stays stuck after uploading, press its **Reset** button.

### Check a board works

With the board plugged in (run this on two boards at the same time to test the radio link):

```bash
python mesh/examples/diagnose.py --board heltec --radio lora --port COM5    # Heltec, LoRa
python mesh/examples/diagnose.py --board heltec --radio espnow --port COM5  # Heltec, ESP-NOW
python mesh/examples/diagnose.py                                            # Feather / ESP32-S3
```

Step 1 should say the firmware is responding and the radio is on. Step 2 should
print the other board's pings. `--port` can be left out when only one board is
plugged in.

## 4. Run it

### Quickest test: two boards, no app

Each command runs one hiker. Use a different `--name`, `--db` and `--port` for each:

```bash
cd mesh
python examples/run_node.py --board heltec --port COM5 --db alice.db --name alice --demo
python examples/run_node.py --board heltec --port COM6 --db bob.db   --name bob   --demo
```

`--demo` keeps the radio on all the time, so the two find each other within a
second or two. Without it they meet in a short window every 20 s, which saves
battery. Each finished sync prints `session with <id> over LoRa ok`. Add
`--verbose` to see every packet.

### Choosing radios (Heltec boards)

| `--radio` | What it does | Use it to |
|---|---|---|
| `auto` (default) | Finds peers over LoRa. When both hikers' positions are known and they're within 60 m, the data moves over ESP-NOW (fast); otherwise over LoRa, with positions sent first. | Normal use |
| `lora` | LoRa only, at any distance | Test LoRa with the boards side by side |
| `espnow` | ESP-NOW only | Test ESP-NOW on its own |

Use the same mode on every board in a test. Add `--multiple-boards` to Heltecs
in `auto` mode when Feather/ESP32-S3 boards (ESP-NOW only) are in the test too.
`--sf 9` gives LoRa more range but makes it slower; every board must use the same `--sf`.

### The full app

1. **Start a server** (one machine; it's needed once to create each account,
   because names must be unique):
   ```bash
   cd application/server
   python server.py --host 0.0.0.0        # Windows, or quick tests: http://<this-ip>:8000
   gunicorn server:app                    # macOS / Linux
   ```
2. **Start the app** on each hiker's computer, with its board:
   ```bash
   cd application/user_app
   python main.py --server http://<server-ip>:8000 --mesh-port COM5 --mesh-board heltec --mesh-demo
   ```
   With a Feather/ESP32-S3, leave out `--mesh-board heltec`. Add
   `--mesh-radio lora` (or `espnow`) to pin the radio, as in the table above.
3. Open **http://127.0.0.1:5000** in a browser. On the **Debug** tab, create the
   device with a name (this is where the server is needed). Then send messages,
   add comments to places on the map, and watch them appear on the other hiker's app.
4. To see data travel over the radio only, restart the apps with
   `--server "" --no-lan` once the accounts exist. Otherwise the server usually
   delivers data first.

**Two hikers on one computer:** give each its own database, web port and board:

```bash
python main.py --db data/alice.db --port 5001 --mesh-port COM5 --mesh-board heltec --mesh-demo
python main.py --db data/bob.db   --port 5002 --mesh-port COM6 --mesh-board heltec --mesh-demo
```

More options (Raspberry Pi with a phone over Bluetooth, an Android phone with
the board plugged in, a public server URL): `application/user_app/README.md`
and `application/server/README.md`.

## 5. Run the tests (no hardware needed)

```bash
cd mesh && python -m unittest discover -s tests      # the mesh protocol, on simulated radios
cd application/user_app && python -m unittest        # the app's database
cd application/server && python -m unittest          # the server
```

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| `no ESP32 board found on USB` | Board not detected: try another cable, install the CP210x driver (Heltec on Windows), or pass `--port COMx`. |
| `several ESP32 boards found` | Pass `--port` with the one to use. |
| `... is already open in another program` | Close the Arduino Serial Monitor, or another `run_node.py`, `diagnose.py` or `main.py` using that board. |
| `NO REPLY FROM FIRMWARE` | Wrong port, or the board isn't flashed with the firmware for `--board`. Press Reset and retry. |
| `diagnose.py` step 2 hears nothing | Both boards need the same firmware, `--radio` and `--sf`. Check the antenna. Start about 1 m apart. |
| Boards see each other but never sync | They may already hold the same data: identical devices skip the exchange. Add something new in one app. |
| Can't create the device in the app | No server reachable: start one (section 4) and pass `--server http://<ip>:8000`. |
| Upload fails / "No serial data received" | Hold PRG (Heltec) or BOOT while uploading. Check Tools → Board and Tools → Port. |

## Where to read more

- `mesh/README.md`: how the sync protocol works, with links to per-file docs in `mesh/docs/`.
- `mesh/docs/lora.md`: the Heltec board, LoRa settings, the 60 m switch, the board protocol.
- `mesh/docs/firmware.md`: the ESP-NOW bridge firmware, and flashing notes for other boards.
- `application/user_app/README.md`: the app's options, database and API.
- `application/server/README.md`: running the server, including a public URL.
