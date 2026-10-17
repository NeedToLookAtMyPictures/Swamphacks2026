"""ESP-NOW link: talks to an ESP32 running firmware/espnow_bridge over USB serial.

The ESP32 is a thin radio bridge; all protocol logic runs here in Python.
Host <-> ESP32 messages are SLIP-framed, first byte is the command:

  host -> esp32
    0x01 SEND         mac[6] + payload   (mac ff:ff:ff:ff:ff:ff = broadcast)
    0x02 SET_PARTNER  device_id u32 LE   (0 = accept everyone)
    0x03 RADIO        u8 (1 = on, 0 = off)
    0x04 GET_INFO

  esp32 -> host
    0x81 RECV         mac[6] + rssi i8 + payload
    0x84 INFO         own mac[6] + STATUS (radio state, channel, last error, counters)
    0x85 LOG          utf-8 text (debug)
"""
from __future__ import annotations

import queue
import struct
import threading

from .link import Link, RxFrame

BROADCAST = b"\xff" * 6

CMD_SEND, CMD_SET_PARTNER, CMD_RADIO, CMD_GET_INFO = 0x01, 0x02, 0x03, 0x04
EVT_RECV, EVT_INFO, EVT_LOG = 0x81, 0x84, 0x85

SLIP_END, SLIP_ESC, SLIP_ESC_END, SLIP_ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD

# Must match `struct Status` in the firmware.
STATUS = struct.Struct("<BBiIIIII")
STATUS_FIELDS = ("radio_on", "channel", "last_err", "tx_ok", "tx_err", "rx", "rx_filtered", "rx_dropped")


def slip_encode(data: bytes) -> bytes:
    out = bytearray([SLIP_END])
    for b in data:
        if b == SLIP_END:
            out += bytes([SLIP_ESC, SLIP_ESC_END])
        elif b == SLIP_ESC:
            out += bytes([SLIP_ESC, SLIP_ESC_ESC])
        else:
            out.append(b)
    out.append(SLIP_END)
    return bytes(out)


class SlipDecoder:
    def __init__(self):
        self._buf = bytearray()
        self._esc = False

    def feed(self, data: bytes) -> list[bytes]:
        frames = []
        for b in data:
            if self._esc:
                self._buf.append(SLIP_END if b == SLIP_ESC_END else SLIP_ESC if b == SLIP_ESC_ESC else b)
                self._esc = False
            elif b == SLIP_ESC:
                self._esc = True
            elif b == SLIP_END:
                if self._buf:
                    frames.append(bytes(self._buf))
                    self._buf.clear()
            else:
                self._buf.append(b)
        return frames


class EspNowSerialLink(Link):
    mtu = 250  # ESP-NOW v1 limit; works with every ESP-IDF version

    def __init__(self, port: str, baud: int = 460800, log=print):
        import serial  # pyserial; imported here so the simulator works without it

        self._ser = serial.Serial(port, baud, timeout=0.05)
        self._rx: queue.Queue[RxFrame] = queue.Queue()
        self._log = log
        self.mac: bytes | None = None
        self.status: dict | None = None  # filled by each INFO reply; see request_status()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # The board keeps state across host restarts: clear any partner filter left
        # over from a session that was interrupted, and make sure the radio is on.
        self.set_partner(None)
        self.set_radio(True)
        self._write(bytes([CMD_GET_INFO]))

    def _write(self, msg: bytes) -> None:
        self._ser.write(slip_encode(msg))

    def _read_loop(self) -> None:
        dec = SlipDecoder()
        while not self._stop.is_set():
            data = self._ser.read(512)
            for msg in dec.feed(data):
                if not msg:
                    continue
                if msg[0] == EVT_RECV and len(msg) >= 8:
                    self._rx.put(RxFrame(msg[8:], msg[1:7], struct.unpack("b", msg[7:8])[0]))
                elif msg[0] == EVT_INFO and len(msg) >= 7:
                    self.mac = msg[1:7]
                    if len(msg) >= 7 + STATUS.size:
                        self.status = dict(zip(STATUS_FIELDS, STATUS.unpack_from(msg, 7)))
                elif msg[0] == EVT_LOG and self._log:
                    self._log("[esp32]", msg[1:].decode(errors="replace"))

    def send(self, payload: bytes, dest: bytes | None = None) -> None:
        if len(payload) > self.mtu:
            raise ValueError(f"frame of {len(payload)} bytes exceeds ESP-NOW limit {self.mtu}")
        self._write(bytes([CMD_SEND]) + (dest or BROADCAST) + payload)

    def poll(self) -> list[RxFrame]:
        out = []
        while True:
            try:
                out.append(self._rx.get_nowait())
            except queue.Empty:
                return out

    def request_status(self) -> None:
        """Ask the board for a fresh INFO reply; .mac and .status update when it arrives."""
        self._write(bytes([CMD_GET_INFO]))

    def set_radio(self, on: bool) -> None:
        self._write(bytes([CMD_RADIO, 1 if on else 0]))

    def set_partner(self, device_id: int | None) -> None:
        self._write(bytes([CMD_SET_PARTNER]) + struct.pack("<I", device_id or 0))

    def close(self) -> None:
        self._stop.set()
        self._reader.join(timeout=1)
        self._ser.close()
