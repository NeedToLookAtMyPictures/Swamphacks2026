# Heltec V3: LoRa + ESP-NOW (firmware/heltec_bridge, heltec_serial.py, lora.py, radio.py)

**Purpose.** Run trailmesh on both radios of a Heltec WiFi LoRa 32 V3
(ESP32-S3 + SX1262, CP2102 USB-UART). LoRa (sub-GHz, miles, slow) finds peers
and does the handshake. ESP-NOW (2.4 GHz, ~60 m, fast) takes over the exchange
when the two hikers are close. The LoRa here is **raw LoRa** (LoRa P2P): boards
talk directly to each other. It is not LoRaWAN, which needs gateways.

> **Status:** compiles for `esp32:esp32:heltec_wifi_lora_32_V3` (core 3.3.12,
> RadioLib 7.8.1). It has **not been run on hardware yet**. The Python side is
> tested against a fake board and the simulator.

## Quick start

1. Arduino IDE: board **Heltec WiFi LoRa 32(V3)**, library **RadioLib** (7.x).
   Flash `firmware/heltec_bridge` to every board. **Attach the LoRa antenna
   first**: transmitting without one can damage the SX1262.
2. Check each radio between two boards (run on both at once):
   `python examples/diagnose.py --board heltec --radio lora --port COM5`, then `--radio espnow`.
3. Run a node: `python examples/run_node.py --board heltec --port COM5 --name alice --demo`,
   or the app: `python main.py --mesh-port COM5 --mesh-board heltec`.

## Radio modes (`--radio` / app `--mesh-radio`)

| Mode | Discovery + handshake | Exchange | Use it to |
|---|---|---|---|
| `auto` (default) | LoRa | ESP-NOW if both positions are known and within 60 m, else LoRa | normal use |
| `lora` | LoRa | LoRa, at any distance | test LoRa with the boards side by side |
| `espnow` | ESP-NOW | ESP-NOW (like espnow_bridge) | test ESP-NOW on its own |

All boards in a test should use the same mode. An `auto` board and a `lora`
board still sync over LoRa: the `lora` board never offers ESP-NOW.

A Heltec in `auto` and a plain ESP32 (espnow_bridge) never sync: the Heltec
discovers over LoRa only. Add `--multiple-boards` to the Heltecs for that (below).

## Mixed boards (`--multiple-boards`)

For tests with Heltecs and plain ESP32s together. Heltec, `auto` mode only
(`Config.espnow_fallback`); the plain ESP32s need nothing new.

1. **LoRa first.** Discovery, the handshake and the 60 m switch work exactly as
   above, for every peer heard on LoRa.
2. **ESP-NOW when LoRa isn't working.** While no exchange found over LoRa has
   succeeded within the last discovery period (20 s; with `--demo`, the last 3
   beacon intervals), the Heltec also turns ESP-NOW on during discovery and
   sends each beacon on both radios. So with a plain ESP32 nearby, ESP-NOW is
   tried at least every other window, even while other Heltecs keep syncing over LoRa.
3. **Which radio an exchange uses.** A peer heard only on ESP-NOW (a plain ESP32,
   or a Heltec whose LoRa doesn't reach) gets an exchange that runs entirely over
   ESP-NOW, handshake included. A peer heard on both uses LoRa, unless the last
   LoRa exchange with it failed: then the next try is over ESP-NOW, and the one
   after that is LoRa's again.

Costs: the ESP-NOW radio is on in discovery windows where LoRa found nothing
(2 s every 20 s), and the 60 m switch only applies to peers heard on LoRa.

```bash
python examples/run_node.py --board heltec --multiple-boards --name alice --demo   # Heltec
python examples/run_node.py --name bob --demo                                       # plain ESP32
python main.py --mesh-port auto --mesh-board heltec --multiple-boards --mesh-demo  # the app, on the Heltec
```

## The 60 m switch (`auto`)

1. Beacons and HELLO / HELLO_ACK always go over LoRa. HELLO and HELLO_ACK carry
   the sender's position (from the app's database, at most `position_max_age`
   = 5 min old) and flags: has a fix, can switch to ESP-NOW.
2. The **responder** decides. If both sides can switch, both positions are
   known, and they are within `espnow_range` (60 m), its HELLO_ACK says "use
   ESP-NOW". Both sides turn ESP-NOW on and send everything after the handshake
   there, with ESP-NOW timings.
3. Otherwise the whole exchange (INV, WANT, DATA) runs over LoRa, with
   **positions (pins) sent first**, then comments, messages and tombstones.
4. ESP-NOW is on only during an ESP-NOW exchange (plus a few seconds after, to
   confirm the last frames). If an ESP-NOW exchange fails (the positions were
   stale, or 60 m was optimistic), that peer gets LoRa for `espnow_retry`
   (5 min).

## LoRa settings (`lora.LoRaParams`, `--sf`)

| Field | Default | Notes |
|---|---|---|
| `freq_hz` | 915 000 000 | US ISM band. The board covers 863–928 MHz. |
| `bw_hz` | 500 000 | 125/250/500 kHz. 500 kHz isn't subject to the FCC's 400 ms dwell limit on a single channel. |
| `sf` | 7 | 7–12. Each step up roughly doubles airtime and adds range. |
| `cr` | 5 | coding rate 4/5 |
| `power_dbm` | 20 | SX1262 max is 22 |

All boards must use the same frequency, bandwidth, SF and coding rate; the host
sets them on every connect. A full 250-byte frame takes 97 ms at SF7 and
307 ms at SF9. In the simulator a LoRa exchange moves about **1.3 KB/s at SF7**
and **0.47 KB/s at SF9**.

## Host protocol

espnow_bridge's commands (see [firmware.md](firmware.md)) drive ESP-NOW unchanged; LoRa adds:

| Code | Direction | Payload |
|---|---|---|
| 0x01 SEND / 0x03 RADIO / 0x81 RECV | | ESP-NOW, exactly as espnow_bridge |
| 0x02 SET_PARTNER | host → board | applies to **both** radios |
| 0x05 CONFIG | host → board | `freq_hz u32, bw_hz u32, sf u8, cr u8, power i8` |
| 0x06 LORA_SEND | host → board | payload (always a broadcast; queued, 16 frames) |
| 0x07 LORA_RADIO | host → board | u8 on/off |
| 0x86 LORA_RECV | board → host | `rssi i8` + payload |
| 0x84 INFO | board → host | MAC + ESP-NOW status (as espnow_bridge) + LoRa status (`radio_on, last_err, tx_ok, tx_err, rx, rx_filtered, freq, bw, sf, cr, power, tx_queued, airtime_ms, deferrals, rx_crc_err`) |

Baud is **115200** (a real UART through the CP2102). Both radios are off at boot.
Opening the port resets the board, so it sends an INFO at boot and
`HeltecSerialLink` re-sends CONFIG, partner and radio states when an INFO
doesn't match what it wants.

Text commands for a serial monitor at 115200: `HELP, ID, STATUS, LORA ON|OFF,
ESPNOW ON|OFF, TX <text>` (LoRa), `ETX <text>` (ESP-NOW), `SF <n>`.

## Firmware behavior (LoRa side)

- **Listen before talk.** Raw LoRa has none, so before each send the firmware
  waits if a packet is arriving (header-valid IRQ flag), then runs channel
  activity detection (CAD). If the channel is busy it listens and retries
  after a random 0.25–1.25 frame airtimes. After 8 busy checks in a row it
  sends anyway.
- **Sync word `0x24`**, so LoRaWAN (0x34), Meshtastic (0x2B) and RadioLib's
  default (0x12) traffic is dropped in hardware.
- **LORA_RADIO off** puts the SX1262 to sleep (it keeps its configuration) and
  drops any queued frames.
- Warns if a full frame is longer than 400 ms at a bandwidth under 500 kHz (US dwell rule).

## Protocol changes for LoRa (protocol v4)

- **HELLO names its target.** Every LoRa frame is a broadcast; without it every
  idle device that heard a HELLO became a responder.
- **HELLO / HELLO_ACK carry position and radio flags** (the 60 m switch).
- **Delayed ACKs** (`Config.ack_delay`): only one side can transmit at a time, so
  the receiver confirms a whole burst with one ACK. ESP-NOW keeps `ack_delay=0`.
- **`Config.lora(params)`** derives the timeouts from airtime, and sets
  `positions_first`.

## TODOs

- Tune `Config.lora()` multipliers on real boards (they come from the simulator).
- Avoid failing (and backing off) on a HELLO to a busy peer: a BUSY reply, or a busy flag in beacons.
- Piggyback ACKs on DATA frames.
- Longer discovery period for LoRa to save power.
- The simulator's listen-before-talk is perfect; real CAD misses some packets.
