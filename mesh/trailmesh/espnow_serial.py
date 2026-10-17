"""ESP-NOW link: talks to an ESP32 running firmware/espnow_bridge over USB.

The ESP32 is a thin radio bridge; all protocol logic runs here in Python.
How the bytes reach the board is a Transport (transport.py), chosen from the
port: pyserial on laptops, termux-usb on Android. This module only speaks the
bridge protocol and is the same for every transport.

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
from .transport import PortInUse, Transport, TransportError, open_transport

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


class LinkLost(ConnectionError):
    """The board is gone and the transport can't reopen it (Android). Restart the program."""


class EspNowSerialLink(Link):
    mtu = 250  # ESP-NOW v1 limit; works with every ESP-IDF version

    def __init__(self, port: str = "auto", baud: int = 460800, log=print, reconnect_interval: float = 1.0):
        """port: see transport.open_transport ("auto", "COM3", "/dev/ttyACM0", "termux-usb", ...).

        Raises TransportError if the board can't be opened at startup. On Android,
        if this program wasn't started by termux-usb, this reruns it under termux-usb
        and exits (see termux_usb.py).
        """
        self.port = port
        self.baud = baud
        self.reconnect_interval = reconnect_interval
        self._log = log
        self._lock = threading.RLock()
        self._transport: Transport | None = None
        self._reconnectable = True
        self._rx: queue.Queue[RxFrame] = queue.Queue()
        self.mac: bytes | None = None
        self.status: dict | None = None  # filled by each INFO reply; see request_status()
        self.dropped_writes = 0  # frames discarded while the board was disconnected
        # What the host wants the board to be doing. Re-sent after every (re)connect,
        # because the board forgets everything when it is unplugged or reset.
        self._radio_wanted = True
        self._partner: int | None = None
        self._open()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @property
    def connected(self) -> bool:
        return self._transport is not None

    @property
    def lost(self) -> bool:
        """True once the board is gone for good: disconnected, and the transport can't reopen it."""
        return self._transport is None and not self._reconnectable

    def _emit(self, *msg) -> None:
        if self._log:
            self._log(*msg)

    def _open(self) -> None:
        t = open_transport(self.port, self.baud)
        with self._lock:
            self._transport = t
            self._reconnectable = t.reconnectable
        # Clear any partner filter left over from an interrupted session, restore
        # the radio state, and ask who we're talking to.
        self._write(bytes([CMD_SET_PARTNER]) + struct.pack("<I", self._partner or 0))
        self._write(bytes([CMD_RADIO, 1 if self._radio_wanted else 0]))
        self._write(bytes([CMD_GET_INFO]))

    def _lost(self, err: Exception) -> None:
        with self._lock:
            t = self._transport
            if t is None:
                return
            self._transport = None
            t.close()
        if self._reconnectable:
            self._emit(f"[link] lost connection to board on {self.port} ({err}); retrying")
        else:
            self._emit(f"[link] lost connection to board on {self.port} ({err}); "
                       "this platform can't reopen it, restart the program")

    def _write(self, msg: bytes) -> None:
        with self._lock:
            t = self._transport
            if t is None:
                self.dropped_writes += 1  # the protocol's retransmits recover these
                return
            try:
                t.write(slip_encode(msg))
            except TransportError as e:
                self._lost(e)

    def _read_loop(self) -> None:
        dec = SlipDecoder()
        busy_reported = False
        while not self._stop.is_set():
            t = self._transport
            if t is None:
                if not self._reconnectable:
                    return  # poll() raises LinkLost from now on
                self._stop.wait(self.reconnect_interval)
                if self._stop.is_set():
                    return
                try:
                    self._open()
                    dec = SlipDecoder()
                    self._emit(f"[link] reconnected to board on {self.port}")
                except PortInUse as e:
                    if not busy_reported:
                        self._emit(f"[link] {e}")
                        busy_reported = True
                except TransportError:
                    pass  # still unplugged
                else:
                    busy_reported = False
                continue
            try:
                data = t.read(512)
            except TransportError as e:
                self._lost(e)
                continue
            for msg in dec.feed(data):
                self._handle(msg)

    def _handle(self, msg: bytes) -> None:
        if not msg:
            return
        if msg[0] == EVT_RECV and len(msg) >= 8:
            self._rx.put(RxFrame(msg[8:], msg[1:7], struct.unpack("b", msg[7:8])[0]))
        elif msg[0] == EVT_INFO and len(msg) >= 7:
            self.mac = msg[1:7]
            if len(msg) >= 7 + STATUS.size:
                self.status = dict(zip(STATUS_FIELDS, STATUS.unpack_from(msg, 7)))
        elif msg[0] == EVT_LOG:
            self._emit("[esp32]", msg[1:].decode(errors="replace"))

    def send(self, payload: bytes, dest: bytes | None = None) -> None:
        if len(payload) > self.mtu:
            raise ValueError(f"frame of {len(payload)} bytes exceeds ESP-NOW limit {self.mtu}")
        self._write(bytes([CMD_SEND]) + (dest or BROADCAST) + payload)

    def poll(self) -> list[RxFrame]:
        """Frames received since the last poll. Raises LinkLost once the board is gone for good."""
        out = []
        while True:
            try:
                out.append(self._rx.get_nowait())
            except queue.Empty:
                break
        if not out and self.lost:
            raise LinkLost(f"lost the board on {self.port}; restart the program to reconnect")
        return out

    def request_status(self) -> None:
        """Ask the board for a fresh INFO reply; .mac and .status update when it arrives."""
        self._write(bytes([CMD_GET_INFO]))

    def set_radio(self, on: bool) -> None:
        self._radio_wanted = on
        self._write(bytes([CMD_RADIO, 1 if on else 0]))

    def set_partner(self, device_id: int | None) -> None:
        self._partner = device_id
        self._write(bytes([CMD_SET_PARTNER]) + struct.pack("<I", device_id or 0))

    def close(self) -> None:
        self._stop.set()
        self._reader.join(timeout=1)
        with self._lock:
            if self._transport is not None:
                self._transport.close()
                self._transport = None
