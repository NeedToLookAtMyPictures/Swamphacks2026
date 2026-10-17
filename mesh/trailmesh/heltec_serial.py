"""Both radios of a Heltec WiFi LoRa 32 V3 running firmware/heltec_bridge, over one USB serial port.

`HeltecSerialLink` owns the port and is itself the ESP-NOW link (same commands
as espnow_bridge). Its `.lora` attribute is the LoRa link. Give both to a Node
(`link=board.lora, fast_link=board`) for the 60 m switch, or just one of them to
pin the board to that radio (see radio.py).

Extra commands and events on top of espnow_serial.py:

  host -> board
    0x05 CONFIG      freq_hz u32, bw_hz u32, sf u8, cr u8, power i8 (lora.CONFIG)
    0x06 LORA_SEND   payload (always a broadcast: LoRa has no addresses)
    0x07 LORA_RADIO  u8 (1 = on, 0 = off)
  board -> host
    0x86 LORA_RECV   rssi i8 + payload
    0x84 INFO        own mac[6] + STATUS (ESP-NOW) + LORA_STATUS

Opening the port resets the board (USB-UART auto-reset). It announces itself
with an INFO; if that shows other radio states or settings than ours, the host
re-sends them.
"""
from __future__ import annotations

import queue
import struct

from .espnow_serial import STATUS, EspNowSerialLink
from .link import Link, RxFrame
from .lora import LoRaParams

CMD_CONFIG, CMD_LORA_SEND, CMD_LORA_RADIO = 0x05, 0x06, 0x07
EVT_LORA_RECV = 0x86

# Follows STATUS in the INFO reply; must match `struct LoRaStatus` in firmware/heltec_bridge.
LORA_STATUS = struct.Struct("<BiIIIIIIBBbBIII")
LORA_STATUS_FIELDS = ("radio_on", "last_err", "tx_ok", "tx_err", "rx", "rx_filtered", "freq_hz", "bw_hz", "sf",
                      "cr", "power_dbm", "tx_queued", "airtime_ms", "deferrals", "rx_crc_err")


class LoRaLink(Link):
    """The board's LoRa radio. Created by HeltecSerialLink; shares its serial port."""

    mtu = 250  # the SX1262 carries up to 255 bytes; trailmesh frames stop at 250

    def __init__(self, board: HeltecSerialLink):
        self._board = board
        self._rx: queue.Queue[RxFrame] = queue.Queue()
        self.radio_wanted = False

    def send(self, payload: bytes, dest: bytes | None = None) -> None:
        if len(payload) > self.mtu:
            raise ValueError(f"frame of {len(payload)} bytes exceeds LoRa frame limit {self.mtu}")
        self._board._write(bytes([CMD_LORA_SEND]) + payload)

    def poll(self) -> list[RxFrame]:
        out = []
        while True:
            try:
                out.append(self._rx.get_nowait())
            except queue.Empty:
                return out

    def set_radio(self, on: bool) -> None:
        self.radio_wanted = on
        self._board._write(bytes([CMD_LORA_RADIO, 1 if on else 0]))

    def set_partner(self, device_id: int | None) -> None:
        self._board.set_partner(device_id)  # one filter in the firmware covers both radios

    @property
    def params(self) -> LoRaParams:
        return self._board.params


class HeltecSerialLink(EspNowSerialLink):
    radio_on_at_start = False  # the Node turns on whichever radio it uses

    def __init__(self, port: str, params: LoRaParams | None = None, baud: int = 115200, log=print,
                 reconnect_interval: float = 1.0):
        self.params = params or LoRaParams()
        self.lora = LoRaLink(self)
        self.lora_status: dict | None = None  # LoRa part of each INFO reply
        super().__init__(port, baud=baud, log=log, reconnect_interval=reconnect_interval)

    def _restore(self) -> None:
        self._write(bytes([CMD_CONFIG]) + self.params.pack())
        super()._restore()
        self._write(bytes([CMD_LORA_RADIO, 1 if self.lora.radio_wanted else 0]))

    def _on_event(self, msg: bytes) -> None:
        if msg[0] == EVT_LORA_RECV and len(msg) >= 2:
            self.lora._rx.put(RxFrame(msg[2:], None, struct.unpack("b", msg[1:2])[0]))

    def _on_info(self, msg: bytes) -> None:
        super()._on_info(msg)
        off = 7 + STATUS.size
        if len(msg) < off + LORA_STATUS.size:
            return
        st = self.lora_status = dict(zip(LORA_STATUS_FIELDS, LORA_STATUS.unpack_from(msg, off)))
        p = self.params
        if (
            bool(st["radio_on"]) != self.lora.radio_wanted
            or bool(self.status["radio_on"]) != self._radio_wanted
            or (st["freq_hz"], st["bw_hz"], st["sf"], st["cr"], st["power_dbm"])
            != (p.freq_hz, p.bw_hz, p.sf, p.cr, p.power_dbm)
        ):
            self._restore()  # the board rebooted (or someone typed commands at it)

    def set_params(self, params: LoRaParams) -> None:
        """Change the LoRa settings. Every board in the mesh must use the same ones."""
        self.params = params
        self._write(bytes([CMD_CONFIG]) + params.pack())
