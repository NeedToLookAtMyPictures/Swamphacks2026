# LoRa: firmware/lora_bridge, lora_serial.py, lora.py

**Purpose.** Run the same trailmesh protocol over the SX1262 LoRa radio of a
Heltec WiFi LoRa 32 V3 (ESP32-S3 + SX1262, CP2102 USB-UART) instead of ESP-NOW.
This is **raw LoRa** (LoRa P2P): boards talk directly to each other. It is
not LoRaWAN, which needs gateways and a network server. Everything above
`Link` (Node, Session, wire format, the app's database) is unchanged.

> **Status:** compiles for `esp32:esp32:heltec_wifi_lora_32_V3` (core 3.3.12,
> RadioLib 7.8.1). It has **not been run on hardware yet**. The Python side is
> tested against a fake board and the simulator's airtime model.

## Quick start

1. Arduino IDE: board **Heltec WiFi LoRa 32(V3)**, library **RadioLib** (7.x).
   Flash `firmware/lora_bridge` to every board. **Attach the antenna first**:
   transmitting without one can damage the SX1262.
2. `python examples/diagnose.py --link lora --port COM5` on two computers (or
   two ports). Each should print the other's pings.
3. `python examples/run_node.py --link lora --port COM5 --name alice --demo`,
   or from the app: `python main.py --mesh-port COM5 --mesh-link lora`.

All boards must use the same frequency, bandwidth, SF and coding rate. The host
sets them on every connect (`--sf`, default 7).

## Radio settings (`lora.LoRaParams`)

| Field | Default | Notes |
|---|---|---|
| `freq_hz` | 915 000 000 | US ISM band. The board covers 863–928 MHz. |
| `bw_hz` | 500 000 | 125/250/500 kHz. 500 kHz isn't subject to the FCC's 400 ms dwell limit on a single channel. |
| `sf` | 7 | 7–12. Each step up roughly doubles airtime and adds range. |
| `cr` | 5 | coding rate 4/5 |
| `power_dbm` | 20 | SX1262 max is 22 |

`LoRaParams.airtime(n)` gives the time on air of an n-byte packet (SX1262
datasheet formula, matches Semtech's calculator). A full 250-byte frame takes
97 ms at SF7 and 307 ms at SF9, both at 500 kHz.

Measured in the simulator, two nodes, one session: about **1.3 KB/s at SF7**
and **0.47 KB/s at SF9**. Text, positions and comments are fine. Large
records take minutes.

## Host protocol

The same SLIP commands as [firmware.md](firmware.md), with these differences:

| | ESP-NOW bridge | LoRa bridge |
|---|---|---|
| SEND | unicast or broadcast | MAC ignored, always broadcast; **queued** (16 frames) because a frame takes 0.1–1 s to send |
| RECV | sender MAC + RSSI | all-zero MAC (LoRa has no addresses) + RSSI. `LoRaSerialLink.poll()` reports `src=None`. |
| CONFIG `0x05` | — | `freq_hz u32, bw_hz u32, sf u8, cr u8, power i8`. Sent before RADIO on at every (re)connect. |
| INFO | MAC + status | MAC + the same status block (`channel` = SF) + `freq, bw, sf, cr, power, tx_queued, airtime_ms, deferrals, rx_crc_err` |
| Baud | 460800 (ignored on native USB) | **115200** (real UART through the CP2102) |
| Boot | silent | sends an INFO. Opening the port resets the board, so `LoRaSerialLink` re-sends CONFIG/partner/RADIO when an INFO shows the radio off or other settings. |

Text commands for a serial monitor at 115200: `HELP, ID, STATUS, ON, OFF, TX <text>, SF <n>`.

## Firmware behavior

- **Listen before talk.** Raw LoRa has none, so before each send the firmware
  (1) waits if a packet is arriving (header-valid IRQ flag), then (2) runs
  channel activity detection (CAD). If the channel is busy it goes back to
  receive and retries after a random 0.25–1.25 frame airtimes. After 8 busy
  checks in a row it sends anyway.
- **Partner filter:** same as ESP-NOW, on the first 4 bytes.
- **Sync word `0x24`**, so LoRaWAN (0x34), Meshtastic (0x2B) and RadioLib's
  default (0x12) traffic is dropped in hardware.
- **RADIO off** puts the SX1262 to sleep (it keeps its configuration) and
  drops any queued frames.
- Warns in the log if a full frame is longer than 400 ms at a bandwidth
  under 500 kHz (US dwell rule).

## Protocol changes LoRa needed

- **HELLO names its target** (protocol v3). Every LoRa frame is a broadcast,
  so before this every idle device that heard a HELLO became a responder.
- **Delayed ACKs** (`Config.ack_delay`): only one side can transmit at a time,
  so the receiver waits for the sender's burst to end and confirms it with one
  cumulative + selective ACK. That's about 3.5× fewer ACK frames. ESP-NOW keeps
  `ack_delay=0` (ACK every frame).
- **`Config.lora(params)`** derives `rto`, `idle_timeout`, `hello_interval`,
  `window`, etc. from the airtime of `params`, with `window_frames=4`.

## Simulator

`SimMedium(clock, airtime=LoRaParams().airtime)` models a half-duplex radio:
one frame at a time per link, a frame occupies the channel for its airtime,
frames that overlap at a receiver are both lost, a radio hears nothing while
it transmits, and listen-before-talk defers while the channel is busy. In a
6-hiker cluster (all in range, positions written every minute, default LoRa
config), every pair synced within 10 minutes.

## TODOs

- Tune `Config.lora()` multipliers on real boards (they come from the simulator).
- Avoid failing (and backing off) on a HELLO to a busy peer: a BUSY reply, or a busy flag in beacons.
- Piggyback ACKs on DATA frames.
- Longer discovery period for LoRa to save power.
- Switch to ESP-NOW for bulk data when a peer is within ~60 m. This needs our
  position in HELLO/HELLO_ACK, and one firmware driving both radios.
- The simulator's listen-before-talk is perfect; real CAD misses some packets.
