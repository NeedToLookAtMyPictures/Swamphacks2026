"""LoRa link: talks to a Heltec V3 (ESP32-S3 + SX1262) running firmware/lora_bridge.

Same USB serial protocol as the ESP-NOW bridge (see espnow_serial.py), so this
is a thin subclass. Differences:

  * Every frame is a broadcast. LoRa has no addresses; receivers identify the
    sender by the device id at the start of the frame. RECV reports no source.
  * One more command:  0x05 CONFIG  freq_hz u32, bw_hz u32, sf u8, cr u8, power i8
    (see lora.CONFIG). It is sent on every (re)connect, before RADIO on.
  * INFO carries extra LoRa counters after the common status block.
  * The board resets when the port is opened (USB-UART auto-reset) and announces
    itself with an INFO. If that shows the radio off, or other settings than
    ours, the host re-sends its state.
"""
from __future__ import annotations

import struct

from .espnow_serial import BROADCAST, CMD_SEND, STATUS, EspNowSerialLink
from .link import RxFrame
from .lora import LoRaParams

CMD_CONFIG = 0x05

# Follows STATUS in the INFO reply; must match `struct Status` in firmware/lora_bridge.
LORA_STATUS = struct.Struct("<IIBBbBIII")
LORA_STATUS_FIELDS = ("freq_hz", "bw_hz", "sf", "cr", "power_dbm", "tx_queued", "airtime_ms", "deferrals",
                      "rx_crc_err")


class LoRaSerialLink(EspNowSerialLink):
    mtu = 250  # the SX1262 carries up to 255 bytes; trailmesh frames stop at 250

    def __init__(self, port: str, params: LoRaParams | None = None, baud: int = 115200, log=print,
                 reconnect_interval: float = 1.0):
        self.params = params or LoRaParams()
        self.lora_status: dict | None = None  # LoRa part of each INFO reply
        super().__init__(port, baud=baud, log=log, reconnect_interval=reconnect_interval)

    def _restore(self) -> None:
        self._write(bytes([CMD_CONFIG]) + self.params.pack())
        super()._restore()

    def _on_info(self, msg: bytes) -> None:
        super()._on_info(msg)
        off = 7 + STATUS.size
        if len(msg) < off + LORA_STATUS.size:
            return
        self.lora_status = dict(zip(LORA_STATUS_FIELDS, LORA_STATUS.unpack_from(msg, off)))
        st = self.lora_status
        board = (st["freq_hz"], st["bw_hz"], st["sf"], st["cr"], st["power_dbm"])
        ours = (self.params.freq_hz, self.params.bw_hz, self.params.sf, self.params.cr, self.params.power_dbm)
        if (self._radio_wanted and not self.status["radio_on"]) or board != ours:
            self._restore()  # the board rebooted (or was configured by hand)

    def send(self, payload: bytes, dest: bytes | None = None) -> None:
        if len(payload) > self.mtu:
            raise ValueError(f"frame of {len(payload)} bytes exceeds LoRa frame limit {self.mtu}")
        self._write(bytes([CMD_SEND]) + BROADCAST + payload)  # LoRa can only broadcast

    def poll(self) -> list[RxFrame]:
        return [RxFrame(f.payload, None, f.rssi) for f in super().poll()]

    def set_params(self, params: LoRaParams) -> None:
        """Change the radio settings. Every board in the mesh must use the same ones."""
        self.params = params
        self._write(bytes([CMD_CONFIG]) + params.pack())
