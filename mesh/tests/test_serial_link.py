"""Exercises EspNowSerialLink end to end against a fake ESP32 pair on pseudo-terminals."""
import os
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trailmesh import Config, Node, Record, RecordStore  # noqa: E402
from trailmesh.espnow_serial import (  # noqa: E402
    BROADCAST, CMD_GET_INFO, CMD_RADIO, CMD_SEND, CMD_SET_PARTNER, EVT_INFO, EVT_RECV, STATUS, SlipDecoder,
    slip_encode,
)

try:
    import pty

    import serial  # noqa: F401
    from trailmesh.espnow_serial import EspNowSerialLink
except ImportError:  # pragma: no cover
    EspNowSerialLink = None


class FakeEsp32Pair:
    """Two fake bridges sharing one fake radio channel, mirroring the firmware's behaviour."""

    def __init__(self):
        self.boards = []
        for i in range(2):
            master, slave = pty.openpty()
            self.boards.append({"fd": master, "port": os.ttyname(slave), "mac": bytes([2, 0, 0, 0, 0, i]),
                                "partner": 0, "radio": True})
        self._stop = False
        self._threads = [threading.Thread(target=self._serve, args=(b,), daemon=True) for b in self.boards]
        for t in self._threads:
            t.start()

    def _serve(self, board):
        dec = SlipDecoder()
        while not self._stop:
            try:
                data = os.read(board["fd"], 4096)
            except OSError:
                return
            for m in dec.feed(data):
                if m[0] == CMD_SEND and board["radio"]:
                    dest, payload = m[1:7], m[7:]
                    for other in self.boards:
                        if other is board or not other["radio"]:
                            continue
                        if dest != BROADCAST and dest != other["mac"]:
                            continue
                        sender = struct.unpack_from("<I", payload)[0]
                        if other["partner"] and sender != other["partner"]:
                            continue
                        os.write(other["fd"], slip_encode(bytes([EVT_RECV]) + board["mac"] + b"\xc4" + payload))
                elif m[0] == CMD_SET_PARTNER:
                    board["partner"] = struct.unpack_from("<I", m, 1)[0]
                elif m[0] == CMD_RADIO:
                    board["radio"] = bool(m[1])
                elif m[0] == CMD_GET_INFO:
                    status = STATUS.pack(board["radio"], 6, 0, 0, 0, 0, 0, 0)
                    os.write(board["fd"], slip_encode(bytes([EVT_INFO]) + board["mac"] + status))

    def close(self):
        self._stop = True


@unittest.skipIf(EspNowSerialLink is None, "pyserial not installed")
class SerialLinkTests(unittest.TestCase):
    def test_slip_roundtrip_with_escapes(self):
        msg = bytes([0xC0, 0xDB, 1, 2, 0xDC, 0xDD, 0xC0])
        self.assertEqual(SlipDecoder().feed(slip_encode(msg) + slip_encode(b"x")), [msg, b"x"])

    def test_two_nodes_over_serial(self):
        fake = FakeEsp32Pair()
        links = [EspNowSerialLink(b["port"], log=None) for b in fake.boards]
        try:
            a = Node(1, links[0], RecordStore(), Config(always_on=True))
            b = Node(2, links[1], RecordStore(), Config(always_on=True))
            rec = Record.locale_update(1, int(time.time()), "spring-1203", "water", "dry")
            img = Record(rec.kind, 1, int(time.time()), bytes(range(256)) * 20, b"photo")
            a.post(rec)
            a.post(img)
            deadline = time.time() + 15
            while time.time() < deadline and not (b.store.has(rec.id) and b.store.has(img.id)):
                a.poll()
                b.poll()
                time.sleep(0.002)
            self.assertTrue(b.store.has(rec.id))
            self.assertEqual(b.store.get(img.id).body, img.body)
            self.assertEqual(links[0].mac, fake.boards[0]["mac"])
            self.assertEqual(links[0].status["channel"], 6)
        finally:
            for lk in links:
                lk.close()
            fake.close()


if __name__ == "__main__":
    unittest.main()
